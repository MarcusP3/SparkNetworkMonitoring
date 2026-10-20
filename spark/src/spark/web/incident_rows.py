"""Incidents as the pages list them: target outages and alert-rule incidents
side by side, in one shape.

The dashboard shows the newest ten; the Alerts page shows what is firing now
and the whole history, paged and filtered. Both build their rows here so an
incident reads the same on either, and acknowledging one is here too: the
button is on both.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import suppressions
from ..engine.state import human_duration
from ..models import AlertIncident, Incident, Target, utcnow

# Where an alert incident came from, by its rule key's prefix (the keys
# alert_state uses: snmp_alerts, storage, credentials, truenas_health,
# proxmox_health, internet).
SOURCES = {
    "cpu": "SNMP", "memory": "SNMP", "temperature": "SNMP", "snmpdown": "SNMP",
    "port": "Port", "busy": "Port",
    "pool": "Storage", "space": "Storage", "drive": "Storage",
    "api": "API", "apidrive": "TrueNAS", "tnalert": "TrueNAS",
    "pveguest": "Proxmox", "pvezfs": "Proxmox", "pvestore": "Proxmox",
    "pvespace": "Proxmox", "pvedisk": "Proxmox",
    "internet": "Internet",
}
TARGET = "Target"
# The source filter's choices, in the order the chips show them.
SOURCE_CHOICES = (TARGET, "SNMP", "Port", "Storage", "API", "TrueNAS", "Proxmox", "Internet")


def source_of(key: str) -> str:
    return SOURCES.get(key.split(":", 1)[0], "Alert")


def _key_is(source: str):  # type: ignore[no-untyped-def]
    """AlertIncident rows from one source, by their key's prefix."""
    heads = [h for h, s in SOURCES.items() if s == source]
    return or_(*(c for h in heads for c in (AlertIncident.key == h,
                                            AlertIncident.key.like(f"{h}:%"))))


def target_row(incident: Incident, name: str, href: str | None = None) -> dict:
    return {
        "kind": "target",
        "id": incident.id,
        "target_name": name,
        "source": TARGET,
        "href": href,
        "suppress": None,
        "opened_at": incident.opened_at,
        "closed_at": incident.closed_at,
        "duration": human_duration(incident.duration_seconds),
        "cause": incident.cause,
        "resolution": incident.resolution,
        "suppressed_by_dependency": incident.suppressed_by_dependency,
        "acknowledged_at": incident.acknowledged_at,
        "ack_note": incident.ack_note,
    }


async def rule_of(session: AsyncSession, row: AlertIncident) -> tuple[str | None, str | None]:
    """The suppression rule an alert incident falls under, and for a TrueNAS
    alert its type."""
    rule = suppressions.rule_of(row.key)
    klass = (await suppressions.klass_of(session, row.key, row.detail)
             if rule == "truenas_alerts" else None)
    return rule, klass


def alert_row(row: AlertIncident, rule: str | None, klass: str | None) -> dict:
    closed = row.closed_at
    suppress = None
    if row.device_id and rule:
        query: dict = {"device": row.device_id, "rule": rule}
        if klass:
            query["detail"] = klass
        suppress = f"/alerts/suppressions?{urlencode(query)}#add"
    return {
        "kind": "alert",
        "id": row.id,
        "target_name": row.title,
        "source": source_of(row.key),
        "href": f"/devices/{row.device_id}" if row.device_id else None,
        "suppress": suppress,
        "opened_at": row.opened_at,
        "closed_at": closed,
        "duration": human_duration((closed - row.opened_at).total_seconds()) if closed else None,
        "cause": row.detail,
        "resolution": row.resolution,
        "suppressed_by_dependency": False,
        "acknowledged_at": row.acknowledged_at,
        "ack_note": row.ack_note,
    }


def _target_href(device_id: int | None) -> str | None:
    return f"/devices/{device_id}" if device_id else None


# --------------------------------------------------------------------------
# The Alerts page
# --------------------------------------------------------------------------


async def firing(session: AsyncSession) -> tuple[list[dict], int]:
    """Everything open now, newest first, and how many open alerts are left
    out because their rule is off for their device (as on the dashboard)."""
    rows = [
        target_row(incident, name, _target_href(device_id))
        for incident, name, device_id in (await session.execute(
            select(Incident, Target.name, Target.device_id)
            .join(Target, Target.id == Incident.target_id)
            .where(Incident.closed_at.is_(None))
        )).all()
    ]
    hidden = await suppressions.hidden_on_dashboard(session)
    left_out = 0
    for row in (await session.execute(
        select(AlertIncident).where(AlertIncident.closed_at.is_(None))
    )).scalars():
        rule, klass = await rule_of(session, row)
        if hidden(row, rule, klass):
            left_out += 1
            continue
        rows.append(alert_row(row, rule, klass))
    rows.sort(key=lambda r: r["opened_at"], reverse=True)
    return rows, left_out


@dataclass
class History:
    rows: list[dict]
    total: int


async def history(session: AsyncSession, *, source: str = "", device_id: int | None = None,
                  offset: int = 0, limit: int = 25) -> History:
    """Ended incidents, newest first: outages and alerts merged. Everything,
    the suppressed included -- this is the record, not the news.

    Two tables, one order: the newest `offset + limit` of each are enough to
    know the page, since nothing further down either can be newer."""
    want = offset + limit
    targets = (select(Incident, Target.name, Target.device_id)
               .join(Target, Target.id == Incident.target_id)
               .where(Incident.closed_at.is_not(None)))
    alerts = select(AlertIncident).where(AlertIncident.closed_at.is_not(None))
    if device_id is not None:
        targets = targets.where(Target.device_id == device_id)
        alerts = alerts.where(AlertIncident.device_id == device_id)
    use_targets = source in ("", TARGET)
    use_alerts = source != TARGET
    if source and source != TARGET:
        alerts = alerts.where(_key_is(source))

    rows: list[dict] = []
    total = 0
    if use_targets:
        total += await session.scalar(select(func.count()).select_from(targets.subquery())) or 0
        rows += [target_row(i, name, _target_href(dev)) for i, name, dev in (await session.execute(
            targets.order_by(Incident.opened_at.desc(), Incident.id.desc()).limit(want))).all()]
    if use_alerts:
        total += await session.scalar(select(func.count()).select_from(alerts.subquery())) or 0
        for row in (await session.execute(
                alerts.order_by(AlertIncident.opened_at.desc(), AlertIncident.id.desc())
                .limit(want))).scalars():
            rows.append(alert_row(row, *await rule_of(session, row)))
    rows.sort(key=lambda r: r["opened_at"], reverse=True)
    return History(rows=rows[offset:want], total=total)


# --------------------------------------------------------------------------
# Acknowledging
# --------------------------------------------------------------------------

# A row's "kind", as the forms post it: which table its id is in.
KINDS: dict[str, type[Incident] | type[AlertIncident]] = {"target": Incident, "alert": AlertIncident}


async def acknowledge(session: AsyncSession, kind: str, incident_id: int, note: str) -> bool:
    """Mark an open incident as seen, with an optional note; again replaces
    the note. An incident that has closed meanwhile is left as it ended.
    Nothing is sent: it says so on the pages, and the recovery still goes
    out when it ends."""
    row = await session.get(KINDS[kind], incident_id)
    if row is None or row.closed_at is not None:
        return False
    row.acknowledged_at = utcnow()
    row.ack_note = note.strip() or None
    return True


async def unacknowledge(session: AsyncSession, kind: str, incident_id: int) -> bool:
    row = await session.get(KINDS[kind], incident_id)
    if row is None or row.closed_at is not None or row.acknowledged_at is None:
        return False
    row.acknowledged_at = None
    row.ack_note = None
    return True


def unacknowledged(rows: list[dict]) -> int:
    return sum(1 for r in rows if not r["closed_at"] and not r["acknowledged_at"])


async def opened_since(session: AsyncSession, since: datetime) -> int:
    """Outages and alerts that began after `since`."""
    a = await session.scalar(select(func.count(Incident.id)).where(Incident.opened_at >= since))
    b = await session.scalar(select(func.count(AlertIncident.id))
                             .where(AlertIncident.opened_at >= since))
    return (a or 0) + (b or 0)
