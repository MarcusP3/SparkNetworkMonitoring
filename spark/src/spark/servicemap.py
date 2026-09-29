"""The service map: every device in its place, what it runs, how it is doing.

Built from what SPARK already knows -- nothing here is collected. The shape
comes from the parents set by hand on each device's page (hierarchy.py);
status from the device's targets, or failing that its SNMP polling; services
from the port scan. Devices not placed yet are listed apart, not dropped: a
map that silently leaves out the new thing on the network is the wrong map.

A device counts as placed once it has a role, a parent, or a child.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .discovery.services import services_for
from .models import (
    ROLE_LABELS,
    Device,
    DeviceRole,
    HealthStatus,
    Service,
    SnmpDevice,
    SnmpPoll,
    Target,
)

ROLE_ORDER = list(ROLE_LABELS)

# Worst first: a device with one target down and one up is down.
_SEVERITY = [HealthStatus.DOWN, HealthStatus.DEGRADED, HealthStatus.UNKNOWN,
             HealthStatus.UP, HealthStatus.PAUSED]

# Status colours only for real statuses (brand spec): up, degraded, down.
_KIND = {HealthStatus.UP: "ok", HealthStatus.DEGRADED: "warn", HealthStatus.DOWN: "bad"}


@dataclass
class State:
    label: str
    kind: str          # pill class: ok / warn / bad / neutral
    source: str        # what the answer is based on, for the tooltip


@dataclass
class ServiceRow:
    service: Service
    target: Target | None
    state: State | None


@dataclass
class Node:
    device: Device
    state: State
    role: str
    services: list[ServiceRow]
    children: list[Node] = field(default_factory=list)

    @property
    def count(self) -> int:
        return 1 + sum(child.count for child in self.children)


@dataclass
class ServiceMap:
    roots: list[Node]
    unplaced: list[Node]
    services: list[dict]
    devices: int


def _worst(statuses: list[HealthStatus]) -> HealthStatus:
    return min(statuses, key=_SEVERITY.index)


def device_state(targets: list[Target], poll: SnmpPoll | None) -> State:
    """How a device is doing, from the best source there is for it."""
    active = [t for t in targets if t.enabled]
    if active:
        worst = _worst([HealthStatus(t.status) for t in active])
        label = "checking" if worst is HealthStatus.UNKNOWN else str(worst)
        return State(label, _KIND.get(worst, "neutral"),
                     f"{len(active)} target{'s' if len(active) != 1 else ''}")
    if targets:
        return State("paused", "neutral", "every target is paused")
    if poll is not None and poll.last_polled_at is not None:
        if poll.last_error:
            return State("no answer", "bad", "SNMP polling")
        return State("up", "ok", "SNMP polling")
    return State("not watched", "neutral", "no target and no SNMP polling")


def _target_state(target: Target | None) -> State | None:
    if target is None:
        return None
    if not target.enabled:
        return State("paused", "neutral", target.name)
    status = HealthStatus(target.status)
    label = "checking" if status is HealthStatus.UNKNOWN else str(status)
    return State(label, _KIND.get(status, "neutral"), target.name)


def _sort_key(node: Node):  # type: ignore[no-untyped-def]
    return (ROLE_ORDER.index(node.device.role), node.device.display_name.lower())


async def build(session: AsyncSession, *, query: str = "") -> ServiceMap:
    devices = {
        d.id: d for d in (await session.execute(
            select(Device).where(Device.ignored.is_(False))
        )).scalars()
    }
    targets: dict[int, list[Target]] = {}
    by_service: dict[int, Target] = {}
    for target in (await session.execute(select(Target))).scalars():
        if target.device_id is not None:
            targets.setdefault(target.device_id, []).append(target)
        if target.service_id is not None:
            by_service[target.service_id] = target
    polls = {
        row.device_id: poll
        for row, poll in (await session.execute(
            select(SnmpDevice, SnmpPoll).outerjoin(SnmpPoll, SnmpPoll.snmp_device_id == SnmpDevice.id)
        )).all()
    }
    services = await services_for(session, list(devices))

    nodes = {
        device_id: Node(
            device=device,
            state=device_state(targets.get(device_id, []), polls.get(device_id)),
            role=ROLE_LABELS.get(DeviceRole(device.role), ""),
            services=[ServiceRow(s, by_service.get(s.id), _target_state(by_service.get(s.id)))
                      for s in services.get(device_id, [])],
        )
        for device_id, device in devices.items()
    }

    has_child = {d.parent_device_id for d in devices.values() if d.parent_device_id in devices}
    placed = {
        i for i, d in devices.items()
        if d.role != DeviceRole.UNKNOWN or d.parent_device_id in devices or i in has_child
    }

    # Children onto parents. A parent that is ignored, deleted or part of a
    # hand-made loop leaves its child at the top rather than lost.
    attached: set[int] = set()
    for device_id in placed:
        parent = devices[device_id].parent_device_id
        if parent in nodes and parent != device_id and not _loops(devices, device_id):
            nodes[parent].children.append(nodes[device_id])
            attached.add(device_id)
    for node in nodes.values():
        node.children.sort(key=_sort_key)

    roots = sorted((nodes[i] for i in placed if i not in attached), key=_sort_key)
    unplaced = sorted((nodes[i] for i in devices if i not in placed),
                      key=lambda n: n.device.display_name.lower())

    return ServiceMap(roots=roots, unplaced=unplaced,
                      services=_service_list(nodes, query), devices=len(devices))


def _loops(devices: dict[int, Device], device_id: int) -> bool:
    seen = {device_id}
    current = devices[device_id].parent_device_id
    while current in devices:
        if current in seen:
            return True
        seen.add(current)
        current = devices[current].parent_device_id
    return False


def _service_list(nodes: dict[int, Node], query: str) -> list[dict]:
    """Every service on every device, flat, filtered by `query`.

    Matched against the service's name, its port, the device's name and
    address: "8080", "plex", "nas", "172.16.10." all do what you would expect.
    """
    words = query.lower().split()
    rows = []
    for node in nodes.values():
        device = node.device
        for row in node.services:
            haystack = " ".join(str(x) for x in (
                row.service.name or "", row.service.port, device.display_name,
                device.primary_ip or "", node.role,
            )).lower()
            if all(word in haystack for word in words):
                rows.append({"device": device, "service": row.service, "target": row.target,
                             "state": row.state})
    rows.sort(key=lambda r: (r["service"].port, r["device"].display_name.lower()))
    return rows
