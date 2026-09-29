"""API credentials: Settings -> Credentials.

Keys for devices' own APIs -- TrueNAS now, the UniFi controller later. SNMP
communities and v3 users stay with SNMP (snmp_config.py).

A key is sealed as soon as it arrives and is never shown again; a blank key
field on edit keeps the one saved. A certificate is trusted only by a person
pressing Trust on the fingerprint they were shown (truenas.py). Changing the
address forgets the trusted certificate: a different address is a different
server until someone says otherwise.
"""

from __future__ import annotations

import ipaddress
import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import truenas
from .models import ApiCredential, Device, utcnow
from .vault import SecretUnavailable, Vault

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
        row.last_ok_at = row.last_info = None
        row.last_error = None
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


async def listing(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(ApiCredential).order_by(func.lower(ApiCredential.name)))).scalars()
    out = []
    for row in rows:
        device = await session.get(Device, row.device_id) if row.device_id else None
        out.append({"row": row, "device": device, "kind": KINDS.get(row.kind, row.kind)})
    return out

