"""The cards an integration shows, built one way for its device's page and
for its page under Integrations (routes_integrations.py). The cards
themselves are the macros in templates/_integrations.html."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import hermes_scan, proxmox_health, snmp_alerts, suppressions, truenas_health, unifi
from ..checks import hermes
from ..models import CheckResult, CheckType, Device, DeviceAddress, Target, utcnow


def _ago(when, now) -> str | None:  # type: ignore[no-untyped-def]
    if when is None:
        return None
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{round(seconds / 60)}m ago"
    if seconds < 129600:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


async def api_cards(session: AsyncSession, entries: list[dict]) -> list[dict]:
    """Each credential (credentials.listing or for_device) with what it read:
    TrueNAS's pools, drives and alerts (`truenas`), Proxmox's host, guests,
    storage and drives (`pve`), UniFi's devices (`unifi`)."""
    now = utcnow()
    rules = await snmp_alerts.load(session)
    hot = float(rules.get("drive_celsius") or 50)
    pool_line = float(rules.get("pool_space_percent") or 85)
    lines: dict[int | None, float] = {}
    out = []
    for entry in entries:
        a = dict(entry, ago=_ago(entry["row"].last_checked_at, now))
        row = a["row"]
        a["truenas"] = truenas_health.view(row, hot=hot)
        # Proxmox's storage line: its device's own, if Suppressions moved it.
        if row.kind == "proxmox" and row.device_id not in lines:
            lines[row.device_id] = (await suppressions.for_device(session, row.device_id)).line(
                "pool_space", pool_line)
        a["pve"] = proxmox_health.view(row, line=lines.get(row.device_id, pool_line))
        out.append(a)
    # UniFi: the devices it manages, each linked to its SPARK page by MAC,
    # or by address for one SPARK knows by IP alone (a routed VLAN).
    if any(a["row"].kind == "unifi" for a in out):
        known = (await session.execute(
            select(Device.id, Device.mac, Device.primary_ip).where(Device.ignored.is_(False))
            .order_by(Device.id))).all()
        macs = {mac: ident for ident, mac, _ in known if mac}
        ips: dict[str, int] = {}
        for ident, mac, ip in known:
            if ip and not mac:
                ips.setdefault(ip, ident)
        live = {ident for ident, _, _ in known}
        for ident, ip in (await session.execute(
                select(DeviceAddress.device_id, DeviceAddress.ip))).all():
            if ident in live:
                ips.setdefault(ip, ident)
        for a in out:
            a["unifi"] = unifi.view(a["row"], macs=macs, ips=ips)
    return out


async def hermes_cards(session: AsyncSession, device: Device | None = None) -> list[dict]:
    """The Hermes cards: each Hermes check's, from its last reading
    (checks/hermes.py), then each dashboard the port scan found that no
    Hermes check reads yet (hermes_scan.py). For a device's page, only that
    device's; otherwise every one, each with its device (`device`)."""
    from .routes_targets import devices_for, every

    targets = list((await session.execute(
        select(Target).where(Target.check_type == CheckType.HERMES).order_by(func.lower(Target.name)))).scalars())
    owners = await devices_for(session, targets)
    out = []
    for t in targets:
        owner = owners.get(t.id)
        if device is not None and (owner is None or owner.id != device.id):
            continue
        card = hermes.view(t.readings)
        last = await session.scalar(select(CheckResult).where(CheckResult.target_id == t.id)
                                    .order_by(CheckResult.ts.desc()).limit(1))
        out.append({
            "target": t, "hermes": card, "every": every(t.interval_seconds), "last": last,
            "device": owner,
            # The last check got no answer as Hermes: what is below is older.
            "stale": bool(card and card["read_at"] and last and last.ts > card["read_at"]
                          + timedelta(seconds=1)),
        })
    # Read every 5 minutes without being asked, with Watch to alert on it.
    for service, owner in await hermes_scan.found(session):
        if device is not None and owner.id != device.id:
            continue
        for card in hermes_scan.cards([service], owner.primary_ip):
            # A TCP check on the port already: Watch turns it into this one.
            card["upgrade"] = await hermes_scan.tcp_check(session, service, owner)
            card["device"] = owner
            out.append(card)
    return out
