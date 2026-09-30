"""API credentials: Settings -> Credentials.

Keys for devices' own APIs -- TrueNAS now, the UniFi controller later. SNMP
communities and v3 users stay with SNMP (snmp_config.py).

A key is sealed as soon as it arrives and is never shown again; a blank key
field on edit keeps the one saved. A certificate is trusted only by a person
pressing Trust on the fingerprint they were shown (truenas.py). Changing the
address forgets the trusted certificate: a different address is a different
server until someone says otherwise.

Once a certificate is trusted, SPARK checks each credential every 5 minutes
on its own (the device page and this list show the result), and alerts if
it stops working on two checks in a row -- a revoked key, a replaced
certificate, TrueNAS down -- and again when it works. The rule is under
Settings -> Alerts. A credential with no trusted certificate is never
checked on a schedule: it would only fetch the certificate again.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts, snmp_alerts, suppressions, truenas, truenas_health
from .db import session_scope
from .engine.state import human_duration
from .models import AlertState, ApiCredential, Device, utcnow
from .vault import SecretUnavailable, Vault, vault_for

log = logging.getLogger(__name__)

JOB_ID = "api-credentials"
CHECK_MINUTES = 5
FIRST_CHECK_SECONDS = 45
# Failed checks in a row before an alert: one blip (a reboot, an update) is
# not news.
FAILS_BEFORE_ALERT = 2

# state() -> (pill class, words)
STATES = {
    "ok": ("ok dot", "connected"),
    "waiting": ("neutral", "waiting for you"),
    "bad": ("bad", "not connected"),
    "untested": ("neutral", "not tested"),
}

KINDS = {"truenas": "TrueNAS"}

_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                       r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


class CredentialError(ValueError):
    """A value the form cannot save. The message is for a person."""


def clean_host(raw: str) -> str:
    """An address, host name, or [IPv6], with an optional :port. No scheme,
    no path: SPARK decides the scheme (https only) and the path."""
    host = (raw or "").strip()
    if not host:
        raise CredentialError("Enter the address SPARK should use, or choose a device.")
    if "/" in host or " " in host:
        raise CredentialError(
            f"{host!r} is not an address SPARK can use. Just the address or host name, "
            "with no https:// and no path.")
    port = None
    if host.startswith("["):
        inside, _, rest = host[1:].partition("]")
        try:
            ipaddress.IPv6Address(inside)
        except ValueError:
            raise CredentialError(f"{host!r} is not an address SPARK can use.") from None
        if rest:
            if not rest.startswith(":"):
                raise CredentialError(f"{host!r} is not an address SPARK can use.")
            port = rest[1:]
        name = f"[{inside}]"
    else:
        name, _, port = host.partition(":") if host.count(":") == 1 else (host, "", None)
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if not _HOSTNAME.match(name):
                raise CredentialError(
                    f"{host!r} is not an address SPARK can use. Just the address or host "
                    "name, with no https:// and no path.") from None
    if port is not None and port != "":
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise CredentialError(f"{port!r} is not a port.")
        return f"{name}:{int(port)}"
    return name


async def _device(session: AsyncSession, device_id: int | None) -> Device | None:
    if device_id is None:
        return None
    device = await session.get(Device, device_id)
    if device is None or device.ignored:
        raise CredentialError("That device is not on the list any more.")
    return device


async def _name(session: AsyncSession, name: str, *, not_id: int | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise CredentialError("Give it a name.")
    taken = await session.scalar(select(ApiCredential.id).where(
        func.lower(ApiCredential.name) == name.lower(),
        ApiCredential.id != (not_id or 0)))
    if taken is not None:
        raise CredentialError(f"There is already a credential called {name!r}.")
    return name


async def add(session: AsyncSession, vault: Vault, *, kind: str, name: str,
              device_id: int | None, host: str, api_key: str) -> ApiCredential:
    if kind not in KINDS:
        raise CredentialError("Choose what kind of API this is.")
    name = await _name(session, name)
    device = await _device(session, device_id)
    address = clean_host(host or (device.primary_ip if device else "") or "")
    key = (api_key or "").strip()
    if not key:
        raise CredentialError("Paste the API key.")
    row = ApiCredential(kind=kind, name=name, device_id=device.id if device else None,
                        host=address, key_sealed=vault.seal(key))
    session.add(row)
    await session.flush()
    return row


async def update(session: AsyncSession, vault: Vault, row: ApiCredential, *, name: str,
                 device_id: int | None, host: str, api_key: str) -> None:
    """A blank key keeps the saved one. A new address forgets the trusted
    certificate; so does a new key, since it may be for another box."""
    row.name = await _name(session, name, not_id=row.id)
    device = await _device(session, device_id)
    address = clean_host(host or (device.primary_ip if device else "") or "")
    key = (api_key or "").strip()
    if address != row.host or key:
        row.cert_sha256 = row.pending_sha256 = None
        row.last_ok_at = row.last_info = row.readings = None
        row.last_error = None
        # No longer checked until trusted again: an alert about the old
        # address or key would otherwise stand for ever.
        await forget_alert(session, row.id)
    row.device_id = device.id if device else None
    row.host = address
    if key:
        row.key_sealed = vault.seal(key)
    await session.flush()


async def test(session: AsyncSession, vault: Vault, row: ApiCredential) -> bool:
    """Connect and log in, recording the result on the row. True if it worked.

    With no certificate trusted yet this only fetches the certificate, so the
    page can show its fingerprint; the key is not sent.
    """
    try:
        key = vault.open(row.key_sealed)
    except SecretUnavailable as exc:
        row.last_checked_at, row.last_error = utcnow(), str(exc)
        return False
    try:
        info = await truenas.test(row.host, key, row.cert_sha256)
    except truenas.CertificateNotTrusted as exc:
        row.pending_sha256 = exc.fingerprint
        row.last_checked_at, row.last_error = utcnow(), str(exc)
        return False
    except truenas.TrueNASError as exc:
        row.last_checked_at, row.last_error = utcnow(), str(exc)[:1000]
        return False
    now = utcnow()
    row.last_checked_at = row.last_ok_at = now
    row.last_error = row.pending_sha256 = None
    row.last_info = {"version": info.version, "hostname": info.hostname}
    # Each part only if TrueNAS answered it: one it would not leaves the last
    # reading of that part standing.
    readings = dict(row.readings or {})
    for part, value in info.readings.items():
        if value is not None:
            readings[part] = value
    readings["read_at"] = now.isoformat()
    row.readings = readings
    return True


async def trust(session: AsyncSession, vault: Vault, row: ApiCredential,
                fingerprint: str) -> bool:
    """Trust the certificate the page showed -- only if it is still the one
    last seen, so a person never trusts one they were not shown -- then Test."""
    shown = (fingerprint or "").strip().upper()
    if not row.pending_sha256 or shown != row.pending_sha256:
        raise CredentialError("That certificate is no longer the one TrueNAS presents. "
                              "Test again and check the new fingerprint.")
    row.cert_sha256, row.pending_sha256 = row.pending_sha256, None
    return await test(session, vault, row)


def state(row: ApiCredential) -> str:
    """ok, waiting (a certificate to check before anything is sent), bad,
    or untested."""
    if row.last_error:
        return "waiting" if row.pending_sha256 and not row.cert_sha256 else "bad"
    return "ok" if row.last_ok_at else "untested"


def _entry(row: ApiCredential, device: Device | None) -> dict:
    key = state(row)
    pill, words = STATES[key]
    return {"row": row, "device": device, "kind": KINDS.get(row.kind, row.kind),
            "state": key, "pill": pill, "words": words,
            "scheduled": row.enabled and row.cert_sha256 is not None}


async def listing(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(ApiCredential).order_by(func.lower(ApiCredential.name)))).scalars()
    out = []
    for row in rows:
        device = await session.get(Device, row.device_id) if row.device_id else None
        out.append(_entry(row, device))
    return out


async def for_device(session: AsyncSession, device: Device) -> list[dict]:
    rows = (await session.execute(
        select(ApiCredential).where(ApiCredential.device_id == device.id)
        .order_by(func.lower(ApiCredential.name))
    )).scalars()
    return [_entry(row, device) for row in rows]


# --------------------------------------------------------------------------
# Checked on a schedule, and the alert
# --------------------------------------------------------------------------


def _alert_key(cred_id: int) -> str:
    return f"api:{cred_id}"


async def forget_alert(session: AsyncSession, cred_id: int) -> None:
    """Drop its alert states, silently: removed, or no longer checked. The
    API's own, and its drives' and TrueNAS alerts' (truenas_health.py)."""
    found = await session.get(AlertState, _alert_key(cred_id))
    if found is not None:
        await session.delete(found)
    await snmp_alerts.close_incidents(session, utcnow(), key=_alert_key(cred_id),
                                      resolution=snmp_alerts.GONE)
    await truenas_health.forget(session, cred_id)


async def check_all(config) -> int:  # type: ignore[no-untyped-def]
    """The scheduler's job. Returns how many worked. Never raises."""
    try:
        async with session_scope() as session:
            ids = list((await session.execute(
                select(ApiCredential.id).where(ApiCredential.enabled.is_(True),
                                               ApiCredential.cert_sha256.is_not(None))
                .order_by(ApiCredential.id)
            )).scalars())
        worked = 0
        for cred_id in ids:
            if await check_one(config, cred_id):
                worked += 1
        return worked
    except Exception:  # noqa: BLE001 - the scheduler must keep running
        log.exception("Checking API credentials failed")
        return 0


async def check_one(config, cred_id: int) -> bool:  # type: ignore[no-untyped-def]
    async with session_scope() as session:
        row = await session.get(ApiCredential, cred_id)
        if row is None or not row.enabled or row.cert_sha256 is None:
            return False
        before = dict(row.readings or {})
        worked = await test(session, vault_for(config), row)
        now = utcnow()
        await evaluate(session, row, worked, now)
        if worked:
            await truenas_health.evaluate(session, row, before, now)
    return worked


async def evaluate(session: AsyncSession, row: ApiCredential, worked: bool,
                   now: datetime) -> None:
    key = _alert_key(row.id)
    rules = await snmp_alerts.load(session)
    settings = await alerts.load(session)
    device = await session.get(Device, row.device_id) if row.device_id else None
    sending = settings.get("enabled", True) and not (
        device is not None and await alerts.is_muted(session, device_id=device.id))
    name = device.display_name if device else row.name
    what = KINDS.get(row.kind, row.kind)
    on = bool(rules.get("api_down", True)) and not (
        await suppressions.for_device(session, row.device_id)).off("api_down")
    subject = f"{name}: SPARK cannot use the {what} API"
    body = f"`{row.host}` — {row.last_error or 'no answer'}"
    out = await snmp_alerts.step(session, key, breached=not worked, cleared=worked, value=None,
                                 now=now, hold=timedelta(0), min_polls=FAILS_BEFORE_ALERT,
                                 interval=CHECK_MINUTES * 60,
                                 record=snmp_alerts.Record(row.device_id, subject, body) if on else None)
    if out.fired and on and sending:
        await alerts.enqueue(
            session, kind="api_down", tone="bad", subject=subject, body=body,
            dedupe_key=f"rule:{key}:{out.since.isoformat()}:fire")
        await snmp_alerts._mark_notified(session, key)
    elif out.cleared and out.notified and settings.get("notify_on_recovery", True) and sending:
        took = human_duration((now - out.since).total_seconds())
        await alerts.enqueue(
            session, kind="api_ok", tone="ok",
            subject=f"{name}: the {what} API is working again",
            body=f"`{row.host}` — it was not for {took}.",
            dedupe_key=f"rule:{key}:{out.since.isoformat()}:clear")

