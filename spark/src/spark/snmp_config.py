"""SNMP credential profiles and the devices that use them.

The counterpart to `subnets.py`: validation, storage and the one piece of work
the Settings page can ask for -- Test, which runs the capability probe against
a device and records what it answered.

Two rules run through all of it:

  * **Secrets go in sealed and never come back out to a page.** A profile
    form is rendered with its secret fields empty; a blank secret on edit
    means "keep what is stored". So the only way a community string or key
    appears in HTML is if you are typing it.
  * **No database connection is held across the network.** Test reads what it
    needs, ends its transaction, probes, and writes the result afterwards. A
    probe against a dead device takes the full timeout, and an open read
    transaction for that long pins a WAL snapshot (so checkpoints cannot
    complete past it) and a pooled connection, for nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .collectors import SnmpCollector, SnmpCredential
from .collectors.snmp import AUTH_PROTOCOLS, PRIV_PROTOCOLS
from .models import Device, SnmpDevice, SnmpProfile, utcnow
from .vault import SecretUnavailable, Vault

log = logging.getLogger(__name__)

VERSIONS = ("v2c", "v3")

# RFC 3414 turns a v3 password into a key with a hash that is defined for eight
# or more characters; net-snmp refuses anything shorter outright. Better to say
# so here than to have the device answer with a digest error.
MIN_V3_KEY = 8

MAX_SECRET = 255
MAX_NAME = 64


class ProfileError(ValueError):
    """Something a person typed that cannot be saved. The message is for them."""


@dataclass
class ProfileInput:
    """What the profile form submits, before any of it is trusted."""

    name: str = ""
    version: str = "v2c"
    community: str = ""
    username: str = ""
    auth_protocol: str = "SHA"
    auth_key: str = ""
    priv_protocol: str = "AES"   # "" or "none" for authNoPriv
    priv_key: str = ""
    port: str = "161"


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _port(raw: str) -> int:
    try:
        port = int(_clean(raw) or "161")
    except ValueError:
        raise ProfileError(f"{raw!r} is not a port number.") from None
    if not 1 <= port <= 65535:
        raise ProfileError(f"{port} is not a port number.")
    return port


def _secret(value: str, label: str) -> str:
    # Not stripped: a community string with a trailing space is a different
    # community string, and "helpfully" trimming it is how a working credential
    # becomes one that never matches.
    if len(value) > MAX_SECRET:
        raise ProfileError(f"The {label} is longer than {MAX_SECRET} characters.")
    return value


async def _name_taken(session: AsyncSession, name: str, exclude_id: int | None) -> bool:
    stmt = select(SnmpProfile.id).where(func.lower(SnmpProfile.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(SnmpProfile.id != exclude_id)
    return (await session.scalar(stmt)) is not None


async def save_profile(
    session: AsyncSession,
    vault: Vault,
    data: ProfileInput,
    profile_id: int | None = None,
) -> SnmpProfile:
    """Create a profile, or update one. Raises ProfileError with a message to show.

    Validates everything before touching anything. An earlier version assigned
    fields as it went, so a failure halfway through an edit left the profile
    half-changed in the session -- and the error page, rendering from that
    session, showed the half-changed values. Undoing it with a rollback was
    worse: that expires every object in the session, the signed-in user
    included, and the page then crashed reading a name. Nothing to undo is the
    fix.

    On update, a blank secret keeps the stored one -- the form never shows the
    current value, so blank is the only thing it can honestly send back.
    """
    existing: SnmpProfile | None = None
    if profile_id is not None:
        existing = await session.get(SnmpProfile, profile_id)
        if existing is None:
            raise ProfileError("That profile no longer exists.")

    # ---- validate: every value decided here, nothing assigned yet ----
    name = _clean(data.name)
    if not name:
        raise ProfileError("Give the profile a name.")
    if len(name) > MAX_NAME:
        raise ProfileError(f"Keep the name under {MAX_NAME} characters.")
    if await _name_taken(session, name, profile_id):
        raise ProfileError(f"There is already a profile called {name!r}.")

    version = _clean(data.version).lower()
    if version not in VERSIONS:
        raise ProfileError("Choose SNMP v2c or v3.")
    port = _port(data.port)

    # Each secret resolves to: a new plaintext to seal, or KEEP, or None.
    keep = object()
    fields: dict = {"name": name, "version": version, "port": port}

    if version == "v2c":
        community = _secret(data.community, "community string")
        if not community and not (existing and existing.community_sealed):
            raise ProfileError("Enter the community string.")
        secrets = {"community": community or keep, "auth_key": None, "priv_key": None}
        # Switching from v3: those secrets are no longer credentials for
        # anything, and ciphertext nobody uses is risk kept for no reason.
        fields.update(username=None, auth_protocol=None, priv_protocol=None)
    else:
        username = _clean(data.username)
        if not username:
            raise ProfileError("Enter the SNMPv3 user name.")
        auth_protocol = _clean(data.auth_protocol).upper()
        if auth_protocol not in AUTH_PROTOCOLS:
            raise ProfileError("Choose an authentication protocol from the list.")

        auth_key = _secret(data.auth_key, "authentication password")
        if auth_key and len(auth_key) < MIN_V3_KEY:
            raise ProfileError(
                f"The authentication password needs at least {MIN_V3_KEY} "
                "characters -- SNMPv3 cannot derive a key from a shorter one."
            )
        if not auth_key and not (existing and existing.auth_key_sealed):
            raise ProfileError("Enter the authentication password.")

        priv_protocol: str | None = _clean(data.priv_protocol).upper()
        if priv_protocol in ("", "NONE"):
            # authNoPriv. Allowed -- some agents offer nothing better -- and
            # the page says plainly what it means: authenticated, not private.
            priv_protocol, priv_value = None, None
        else:
            if priv_protocol not in PRIV_PROTOCOLS:
                raise ProfileError("Choose a privacy protocol from the list.")
            priv_key = _secret(data.priv_key, "privacy password")
            if priv_key and len(priv_key) < MIN_V3_KEY:
                raise ProfileError(
                    f"The privacy password needs at least {MIN_V3_KEY} characters."
                )
            if not priv_key and not (existing and existing.priv_key_sealed):
                raise ProfileError("Enter the privacy password.")
            priv_value = priv_key or keep

        secrets = {"community": None, "auth_key": auth_key or keep, "priv_key": priv_value}
        fields.update(username=username, auth_protocol=auth_protocol,
                      priv_protocol=priv_protocol)

    # ---- apply: nothing below can fail validation ----
    profile = existing or SnmpProfile()
    for attr, value in fields.items():
        setattr(profile, attr, value)
    for secret, value in secrets.items():
        column = f"{secret}_sealed"
        if value is keep:
            continue
        setattr(profile, column, vault.seal(value) if value else None)

    if existing is None:
        session.add(profile)
    await session.flush()
    return profile


async def delete_profile(session: AsyncSession, profile_id: int) -> None:
    profile = await session.get(SnmpProfile, profile_id)
    if profile is None:
        return
    in_use = await session.scalar(
        select(func.count(SnmpDevice.id)).where(SnmpDevice.profile_id == profile_id)
    )
    if in_use:
        raise ProfileError(
            f"{profile.name!r} is used by {in_use} device(s). Move them to another "
            "profile or remove them first."
        )
    await session.delete(profile)


def credential_for(profile: SnmpProfile, vault: Vault, *, timeout: float = 3.0,
                   retries: int = 1) -> SnmpCredential:
    """Unseal a profile into what the collector takes. Raises SecretUnavailable."""
    if profile.version == "v3":
        return SnmpCredential(
            version="v3",
            username=profile.username or "",
            auth_protocol=profile.auth_protocol or "SHA",
            auth_key=vault.open(profile.auth_key_sealed) if profile.auth_key_sealed else "",
            priv_protocol=profile.priv_protocol or "",
            priv_key=vault.open(profile.priv_key_sealed) if profile.priv_key_sealed else "",
            port=profile.port,
            timeout=timeout,
            retries=retries,
        )
    return SnmpCredential(
        version="v2c",
        community=vault.open(profile.community_sealed) if profile.community_sealed else "",
        port=profile.port,
        timeout=timeout,
        retries=retries,
    )


def security_level(profile: SnmpProfile) -> str:
    """How the Settings page describes what a profile actually protects."""
    if profile.version == "v2c":
        return "v2c · community in clear text"
    if profile.priv_protocol:
        return f"v3 authPriv · {profile.auth_protocol} / {profile.priv_protocol}"
    return f"v3 authNoPriv · {profile.auth_protocol} · payload in clear text"


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------


async def add_device(session: AsyncSession, device_id: int, profile_id: int) -> SnmpDevice:
    device = await session.get(Device, device_id)
    if device is None:
        raise ProfileError("That device no longer exists.")
    if await session.get(SnmpProfile, profile_id) is None:
        raise ProfileError("Choose a profile.")
    existing = await session.scalar(select(SnmpDevice).where(SnmpDevice.device_id == device_id))
    if existing is not None:
        raise ProfileError(f"{device.display_name} is already on the list.")
    row = SnmpDevice(device_id=device_id, profile_id=profile_id, enabled=True)
    session.add(row)
    await session.flush()
    return row


async def set_device_profile(session: AsyncSession, row_id: int, profile_id: int) -> None:
    row = await session.get(SnmpDevice, row_id)
    if row is None:
        return
    if await session.get(SnmpProfile, profile_id) is None:
        raise ProfileError("Choose a profile.")
    if row.profile_id != profile_id:
        row.profile_id = profile_id
        # The old result was for the old credentials; keeping it would make the
        # page claim something nobody has checked.
        row.last_checked_at = None
        row.last_ok_at = None
        row.last_error = None
        row.last_probe = None


async def remove_device(session: AsyncSession, row_id: int) -> None:
    row = await session.get(SnmpDevice, row_id)
    if row is not None:
        await session.delete(row)


async def list_devices(session: AsyncSession) -> list[tuple[SnmpDevice, Device, SnmpProfile]]:
    rows = await session.execute(
        select(SnmpDevice, Device, SnmpProfile)
        .join(Device, Device.id == SnmpDevice.device_id)
        .join(SnmpProfile, SnmpProfile.id == SnmpDevice.profile_id)
        .order_by(Device.primary_ip)
    )
    return list(rows.all())


async def list_profiles(session: AsyncSession) -> list[SnmpProfile]:
    return list((await session.execute(select(SnmpProfile).order_by(SnmpProfile.name))).scalars())


# --------------------------------------------------------------------------
# Test
# --------------------------------------------------------------------------


def _probe_summary(report) -> dict:  # type: ignore[no-untyped-def]
    """The parts of a ProbeReport the page shows, as plain JSON."""
    return {
        "reachable": report.reachable,
        "error": report.error,
        "sys_name": report.sys_name,
        "sys_descr": (report.sys_descr or "")[:240] or None,
        "vendor": report.vendor,
        "uptime": report.uptime_human,
        "duration": round(report.duration_seconds, 2),
        "capabilities": [
            {"label": c.label, "supported": c.supported, "sample": c.sample,
             "count": c.sample_count, "notes": c.notes}
            for c in report.capabilities
        ],
    }


async def probe_device(session: AsyncSession, vault: Vault, row_id: int,
                      *, timeout: float = 3.0) -> dict:
    """Probe one device with its profile and record what it answered.

    Ends its transaction before the probe. Only reads have happened by then,
    so this is not about the write lock -- WAL readers do not block writers --
    but an open read transaction held for a whole timeout pins a WAL snapshot
    and a pooled connection while waiting on a device that may never answer.
    """
    row = await session.get(SnmpDevice, row_id)
    if row is None:
        raise ProfileError("That device is no longer on the list.")
    device = await session.get(Device, row.device_id)
    profile = await session.get(SnmpProfile, row.profile_id)
    address = device.primary_ip if device else None

    summary: dict
    if not address:
        summary = {"reachable": False, "error": "This device has no address to ask.",
                   "capabilities": []}
    else:
        try:
            credential = credential_for(profile, vault, timeout=timeout, retries=1)
        except SecretUnavailable as exc:
            summary = {"reachable": False, "error": str(exc), "capabilities": []}
        else:
            await session.commit()  # end the read transaction before the network
            collector = SnmpCollector(address, credential)
            try:
                summary = _probe_summary(await collector.probe())
            except Exception as exc:  # noqa: BLE001 - a Test must report, never crash the page
                log.exception("SNMP test of %s failed", address)
                summary = {"reachable": False, "error": f"{type(exc).__name__}: {exc}",
                           "capabilities": []}
            finally:
                await collector.close()
            row = await session.get(SnmpDevice, row_id)
            if row is None:  # removed while we were asking
                return summary

    now = utcnow()
    row.last_checked_at = now
    row.last_probe = summary
    if summary.get("reachable"):
        row.last_ok_at = now
        row.last_error = None
    else:
        row.last_error = summary.get("error") or "No response."
    return summary
