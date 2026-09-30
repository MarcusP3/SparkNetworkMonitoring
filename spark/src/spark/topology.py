"""Where each device is plugged in, worked out from what switches report.

Every 15 minutes each device on the SNMP list is asked for its MAC table
(BRIDGE-MIB / Q-BRIDGE-MIB: which MAC was learned on which port) and its LLDP
neighbours (identity.py reads them, `store` keeps them). From those:

  1. **The root** is the gateway: the device whose role is Gateway, or else
     the polled device that holds the most addresses of its own (a router
     has one per VLAN). Without one there is no "up", and nothing is worked
     out -- the map page says so.
  2. **Each switch's uplink** is the port it learned the gateway's MAC on.
     Every other port leads away from the gateway.
  3. **A device is on the deepest switch that sees it on a downstream port.**
     Every switch between it and the gateway sees it; the one nearest it is
     the one that the others also see downstream. Its port is recorded.
  4. **Behind a device on a port.** When one switch port has several devices
     on it and exactly one of them is infrastructure -- polled over SNMP, or
     a gateway, switch, access point or server by role -- the others are
     behind it: wireless clients behind their access point, VMs behind their
     host, whatever sits behind an unmanaged switch. Two infrastructure
     devices on one port (a wired and a mesh access point) cannot be told
     apart this way; everything there stays on the switch, unless a parent
     set by hand already says which is below which.
  5. **LLDP, where a switch has it,** is exact: a neighbour heard on the
     uplink is above, one heard on any other port is below. It overrides 3.
  6. **A switch no other switch sees below it** hangs off the gateway. Any
     other device **seen only on uplinks** -- upstream of every switch that
     sees it -- is on the gateway itself, or on something SNMP cannot see.

What happens with it depends on the map mode (Preferences, and asked at
setup):

  * **Manual** (the default): shown as suggestions on the map, to accept or
    dismiss, and on each device's page as where SNMP sees it.
  * **Automatic**: applied after every read (`apply_automatic`). A device
    automatic placed follows SNMP when it moves; one placed by hand --
    including by Accept -- is never touched (MapAuto keeps which is which).
    "Not right" still works, and takes the device back off.

In both, a parent set by hand is never replaced, and roles are only ever
filled in where none is set. `wipe` starts the map over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import hierarchy, identity
from .collectors.snmp import FdbEntry, LldpNeighbour, usable_mac
from .db import get_setting, save_setting
from .models import (
    ROLE_LABELS,
    Device,
    DeviceRole,
    MapAuto,
    SnmpDevice,
    SnmpInterface,
    SnmpNeighbour,
    utcnow,
)

log = logging.getLogger(__name__)

FDB, SELF, LLDP = "fdb", "self", "lldp"
DISMISSED = "map_dismissed"
MAX_DISMISSED = 1000

# Devices that other devices can be behind on a switch port.
# A NAS counts: its apps and VMs can sit behind it on one switch port.
INFRA_ROLES = {DeviceRole.GATEWAY, DeviceRole.SWITCH, DeviceRole.ACCESS_POINT, DeviceRole.HOST,
               DeviceRole.NAS}


# --------------------------------------------------------------------------
# Storing what switches said
# --------------------------------------------------------------------------


async def store(session: AsyncSession, row_id: int, *, fdb: list[FdbEntry] | None,
                lldp: list[LldpNeighbour] | None) -> None:
    """Replace this device's MAC table and LLDP rows. None: not answered,
    keep the last ones (identity.store does the same for addresses)."""
    now = utcnow()
    if fdb is not None:
        await session.execute(delete(SnmpNeighbour).where(
            SnmpNeighbour.snmp_device_id == row_id, SnmpNeighbour.kind.in_([FDB, SELF])))
        session.add_all(SnmpNeighbour(
            snmp_device_id=row_id, kind=SELF if e.own else FDB, if_index=e.if_index,
            mac=e.mac, vlan=e.vlan, seen_at=now) for e in fdb)
    if lldp is not None:
        await session.execute(delete(SnmpNeighbour).where(
            SnmpNeighbour.snmp_device_id == row_id, SnmpNeighbour.kind == LLDP))
        session.add_all(SnmpNeighbour(
            snmp_device_id=row_id, kind=LLDP, if_index=n.if_index, mac=n.mac,
            name=(n.name or "")[:255] or None, port=n.port, seen_at=now) for n in lldp)
    await session.flush()


# --------------------------------------------------------------------------
# Working it out -- pure, so every shape of network can be tested
# --------------------------------------------------------------------------


@dataclass
class Net:
    """Everything the inference needs, by device id."""

    macs: dict[int, set[str]]                       # every MAC each device is known by
    tables: dict[int, dict[int, set[str]]]          # switch -> port -> MACs learned there
    lldp: dict[int, list[tuple[int | None, int]]]   # switch -> (port, neighbour device)
    infra: set[int]                                 # devices others can be behind
    root: int | None
    parents: dict[int, int | None] = field(default_factory=dict)   # as set by hand


@dataclass(frozen=True)
class Link:
    parent: int | None     # None: the top of the map (the root)
    via: int | None        # the switch whose report says so
    port: int | None       # its ifIndex
    source: str            # "lldp", "mac", "behind", "top", "uplink", "root"


def infer(net: Net) -> dict[int, Link]:
    """Device id -> where SNMP says it is plugged in. See the module notes."""
    if net.root is None:
        return {}
    root_macs = net.macs.get(net.root, set())
    uplink: dict[int, int | None] = {}
    down: dict[int, dict[str, int]] = {}     # switch -> MAC -> downstream port
    seen_up: set[str] = set()                # MACs some switch learned on its uplink
    for switch, ports in net.tables.items():
        if switch == net.root:
            up = None
        else:
            counts = {port: len(macs & root_macs) for port, macs in ports.items()}
            best = max(counts.values(), default=0)
            if best == 0:
                continue          # never saw the gateway: which way is up is unknown
            up = min(port for port, n in counts.items() if n == best)
        uplink[switch] = up
        down[switch] = {}
        for port in sorted(ports):
            if port == up:
                seen_up |= ports[port]
                continue
            for mac in ports[port]:
                down[switch].setdefault(mac, port)

    def sees(switch: int, device: int) -> int | None:
        hits = [down[switch][m] for m in net.macs.get(device, ()) if m in down[switch]]
        return min(hits) if hits else None

    links: dict[int, Link] = {net.root: Link(None, None, None, "root")}
    for device in sorted(net.macs):
        if device == net.root or not net.macs[device]:
            continue
        candidates = [s for s in down if s != device and sees(s, device) is not None]
        if not candidates:
            if device in uplink:
                # A switch that saw the gateway and that no other switch sees
                # below it: it hangs straight off the gateway.
                links[device] = Link(net.root, device, uplink[device], "top")
            elif net.macs[device] & seen_up:
                links[device] = Link(net.root, None, None, "uplink")
            continue
        # The deepest: the candidate the most other candidates see downstream.
        score = {c: sum(1 for o in candidates if o != c and sees(o, c) is not None)
                 for c in candidates}
        best = max(score.values())
        deepest = [c for c in candidates if score[c] == best]
        if len(deepest) == 1:
            switch = deepest[0]
            links[device] = Link(switch, switch, sees(switch, device), "mac")

    # Behind the one piece of infrastructure on a port.
    by_port: dict[tuple[int, int | None], list[int]] = {}
    for device, link in links.items():
        if link.source == "mac":
            by_port.setdefault((link.parent, link.port), []).append(device)  # type: ignore[arg-type]
    for (switch, port), group in by_port.items():
        members = set(group)
        # One already set by hand below another member is not the one in front.
        front = [d for d in group if d in net.infra and net.parents.get(d) not in members]
        if len(group) < 2 or len(front) != 1:
            continue
        for device in group:
            if device != front[0]:
                links[device] = Link(front[0], switch, port, "behind")

    # LLDP: exact, where there is any. A neighbour heard on a downstream port
    # is below; one heard on the uplink is above. Both ends usually report a
    # link; the parent's side names the port the child is plugged into, so it
    # is the one kept.
    by_lldp: dict[int, Link] = {}
    upward: list[tuple[int, Link]] = []
    for switch, heard in sorted(net.lldp.items()):
        for port, neighbour in heard:
            if neighbour == switch:
                continue
            if switch == net.root or (switch in uplink and port != uplink[switch]
                                      and neighbour != net.root):
                by_lldp.setdefault(neighbour, Link(switch, switch, port, "lldp"))
            elif neighbour == net.root or (switch in uplink and port == uplink[switch]):
                upward.append((switch, Link(neighbour, switch, port, "lldp")))
    for switch, link in upward:
        by_lldp.setdefault(switch, link)
    by_lldp.pop(net.root, None)
    links.update(by_lldp)

    return _without_loops(links)


def _without_loops(links: dict[int, Link]) -> dict[int, Link]:
    """Drop anything whose chain of discovered parents comes back to itself
    (stale tables can disagree). A wrong map is worse than a gap."""
    out = {}
    for device, link in links.items():
        seen, current = {device}, link.parent
        looped = False
        while current is not None and current in links:
            if current in seen:
                looped = True
                break
            seen.add(current)
            current = links[current].parent
        if not looped:
            out[device] = link
    return out


# --------------------------------------------------------------------------
# From the database
# --------------------------------------------------------------------------


@dataclass
class Found:
    device: Device
    parent: Device | None
    via: Device | None
    port: str | None
    source: str
    role: DeviceRole | None = None      # suggested, only where the role is not set

    @property
    def key(self) -> str:
        return f"{self.device.id}:{self.parent.id if self.parent else 0}"

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, "") if self.role else ""

    def where(self) -> str:
        """Where SNMP sees it, for a person."""
        if self.source == "root":
            return "the gateway: the top of the map"
        if self.source == "top":
            port = f", {self.port}," if self.port else ""
            return f"its uplink{port} leads to {self.parent.display_name}"  # type: ignore[union-attr]
        if self.source == "uplink":
            return (f"upstream of every switch: on {self.parent.display_name} itself, "  # type: ignore[union-attr]
                    "or on something SNMP cannot see")
        port = f", {self.port}" if self.port else ""
        via = self.via.display_name if self.via else ""
        if self.source == "behind":
            return f"behind {self.parent.display_name} on {via}{port}"  # type: ignore[union-attr]
        if self.source == "lldp":
            return f"LLDP on {via}{port}"
        return f"{via}{port}"


@dataclass
class Discovery:
    root: Device | None
    found: dict[int, Found]
    switches: int                 # polled devices that answered with a MAC table
    reason: str | None = None     # why nothing was worked out

    def suggestions(self, dismissed: set[str]) -> list[Found]:
        """What would change on the map if accepted: a parent where there is
        none, a role where it is not set. Never a parent set by hand."""
        out = []
        for f in self.found.values():
            if f.key in dismissed:
                continue
            new_parent = f.parent is not None and f.device.parent_device_id is None
            new_role = f.role is not None and f.device.role == DeviceRole.UNKNOWN
            if new_parent or new_role:
                out.append(f)
        return sorted(out, key=lambda f: (_depth(self.found, f.device.id),
                                          f.parent.display_name.lower() if f.parent else "",
                                          f.device.display_name.lower()))


def _depth(found: dict[int, Found], device_id: int) -> int:
    depth, current = 0, found.get(device_id)
    while current is not None and current.parent is not None and depth < hierarchy.MAX_DEPTH:
        depth += 1
        current = found.get(current.parent.id)
    return depth


async def discover(session: AsyncSession) -> Discovery:
    devices = {d.id: d for d in (await session.execute(select(Device))).scalars()}
    polled = {row.id: row.device_id for row in (await session.execute(select(SnmpDevice))).scalars()}

    macs: dict[int, set[str]] = {i: {d.mac} for i, d in devices.items() if d.mac and not d.ignored}
    names: dict[tuple[int, int], str] = {}
    for iface in (await session.execute(select(SnmpInterface))).scalars():
        device_id = polled.get(iface.snmp_device_id)
        if device_id in devices and not devices[device_id].ignored:
            if usable_mac(iface.mac):
                macs.setdefault(device_id, set()).add(iface.mac)  # type: ignore[arg-type]
            names[(device_id, iface.if_index)] = iface.name or iface.descr or f"ifIndex {iface.if_index}"  # type: ignore[index]

    tables: dict[int, dict[int, set[str]]] = {}
    heard: list[tuple[int, SnmpNeighbour]] = []
    for row in (await session.execute(select(SnmpNeighbour))).scalars():
        device_id = polled.get(row.snmp_device_id)
        if device_id not in devices or devices[device_id].ignored:
            continue
        if row.kind == SELF and row.mac:
            macs.setdefault(device_id, set()).add(row.mac)
        elif row.kind == FDB and row.mac and row.if_index is not None:
            tables.setdefault(device_id, {}).setdefault(row.if_index, set()).add(row.mac)
        elif row.kind == LLDP:
            heard.append((device_id, row))

    root = await _root(session, devices, set(polled.values()))
    by_mac = {mac: i for i, ms in sorted(macs.items()) for mac in ms}
    by_name = _names(devices)
    lldp: dict[int, list[tuple[int | None, int]]] = {}
    for switch, row in heard:
        neighbour = by_mac.get(row.mac or "") or by_name.get(_short(row.name))
        if neighbour is not None:
            lldp.setdefault(switch, []).append((row.if_index, neighbour))

    infra = {i for i, d in devices.items() if d.role in INFRA_ROLES} | set(polled.values())
    net = Net(macs=macs, tables=tables, lldp=lldp, infra=infra,
              root=root.id if root else None,
              parents={i: d.parent_device_id for i, d in devices.items()})
    links = infer(net)

    found = {}
    for device_id, link in links.items():
        device = devices.get(device_id)
        parent = devices.get(link.parent) if link.parent is not None else None
        # Ignored devices have no MACs in `net`, so are never here, and nor
        # is anything placed under one.
        if device is None or (link.parent is not None and parent is None):
            continue
        role = None
        if device_id == net.root:
            role = DeviceRole.GATEWAY
        elif device_id in tables:
            role = DeviceRole.SWITCH
        found[device_id] = Found(
            device=device, parent=parent,
            via=devices.get(link.via) if link.via is not None else None,
            port=names.get((link.via, link.port)) if link.port is not None else None,  # type: ignore[arg-type]
            source=link.source, role=role)

    reason = None
    if not polled:
        pass          # no SNMP at all: the map is placed by hand, nothing to explain
    elif not tables and not lldp:
        reason = ("No device on the SNMP list has answered with a MAC table or LLDP "
                  "yet. They are read every 15 minutes; a managed switch is what "
                  "provides them.")
    elif root is None:
        reason = ("SPARK cannot tell which device is your gateway. Set its Role to "
                  "Gateway / router on its page, and the map is worked out from there.")
    return Discovery(root=root, found=found, switches=len(tables), reason=reason)


async def _root(session: AsyncSession, devices: dict[int, Device], polled: set[int]) -> Device | None:
    gateways = sorted((d for d in devices.values()
                       if d.role == DeviceRole.GATEWAY and not d.ignored),
                      key=lambda d: (d.id not in polled, d.id))
    if gateways:
        return gateways[0]
    return await _root_by_addresses(session, devices)


async def _root_by_addresses(session: AsyncSession, devices: dict[int, Device]) -> Device | None:
    """The polled device holding the most addresses of its own (two or more,
    and strictly the most): a router has one per VLAN."""
    counts: dict[int, int] = {}
    for owner in (await identity.reports(session)).own.values():
        counts[owner] = counts.get(owner, 0) + 1
    ranked = sorted(((n, -i) for i, n in counts.items()
                     if n >= 2 and i in devices and not devices[i].ignored), reverse=True)
    if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        return devices[-ranked[0][1]]
    return None


def _short(name: str | None) -> str:
    return (name or "").strip().lower().split(".")[0]


def _names(devices: dict[int, Device]) -> dict[str, int]:
    """Host and friendly names, for LLDP neighbours that give only a name.
    A name two devices share matches neither."""
    out: dict[str, int] = {}
    clash: set[str] = set()
    for device in devices.values():
        for name in {_short(device.hostname), _short(device.friendly_name)} - {""}:
            if name in out and out[name] != device.id:
                clash.add(name)
            out.setdefault(name, device.id)
    return {k: v for k, v in out.items() if k not in clash}


# --------------------------------------------------------------------------
# Acting on a suggestion
# --------------------------------------------------------------------------


async def dismissed(session: AsyncSession) -> set[str]:
    return {p for p in (await get_setting(session, DISMISSED)).get("pairs", []) if isinstance(p, str)}


async def suggestions(session: AsyncSession) -> tuple[Discovery, list[Found]]:
    found = await discover(session)
    return found, found.suggestions(await dismissed(session))


async def accept(session: AsyncSession, f: Found) -> bool:
    """Apply one suggestion: the parent if there is none, the role if unset.
    Refused (False) if the parent would make a loop."""
    device = f.device
    changed = False
    if f.parent is not None and device.parent_device_id is None:
        rows = (await session.execute(select(Device.id, Device.parent_device_id))).all()
        if not hierarchy.can_parent({r.id: r.parent_device_id for r in rows},
                                    device.id, f.parent.id):
            return False
        device.parent_device_id = f.parent.id
        changed = True
    if f.role is not None and device.role == DeviceRole.UNKNOWN:
        device.role = f.role
        changed = True
    await session.flush()
    return changed


async def dismiss(session: AsyncSession, f: Found) -> None:
    pairs = [p for p in (await get_setting(session, DISMISSED)).get("pairs", [])
             if isinstance(p, str)]
    if f.key not in pairs:
        pairs.append(f.key)
    await save_setting(session, DISMISSED, {"pairs": pairs[-MAX_DISMISSED:]})


# --------------------------------------------------------------------------
# Map mode: manual or automatic, and starting over
# --------------------------------------------------------------------------

MODE_SETTING = "map"
MODES = ("manual", "automatic")


async def get_mode(session: AsyncSession) -> str:
    mode = (await get_setting(session, MODE_SETTING)).get("mode")
    return mode if mode in MODES else "manual"


async def set_mode(session: AsyncSession, mode: str) -> Applied:
    """ValueError if not a mode. Switching to automatic applies the map now,
    rather than at the next 15-minute read."""
    if mode not in MODES:
        raise ValueError("Choose Manual or Automatic.")
    value = await get_setting(session, MODE_SETTING)
    value["mode"] = mode
    await save_setting(session, MODE_SETTING, value)
    return await apply_automatic(session)


@dataclass
class Applied:
    placed: int = 0     # had no parent, now has one
    moved: int = 0      # automatic's own, moved to where SNMP now sees it
    roles: int = 0


async def apply_automatic(session: AsyncSession) -> Applied:
    """Automatic mode's pass. Does nothing in manual mode.

    Top of the map first, so each parent is in place before what hangs off
    it. For each place SNMP gives:
      * no parent and never placed by automatic: placed;
      * placed by automatic and still where it put it: moved if SNMP now
        sees it elsewhere;
      * anything else is a person's decision and is left alone -- a parent
        set by hand, or one automatic set and a person then changed or
        cleared.
    "Not right" answers are respected, and a move that would make a loop is
    skipped. Roles are filled in only where none is set.
    """
    done = Applied()
    if await get_mode(session) != "automatic":
        return done
    discovery = await discover(session)
    rejected = await dismissed(session)
    autos = {row.device_id: row for row in (await session.execute(select(MapAuto))).scalars()}
    parents = {r.id: r.parent_device_id for r in
               (await session.execute(select(Device.id, Device.parent_device_id))).all()}
    order = sorted(discovery.found.values(), key=lambda f: _depth(discovery.found, f.device.id))
    for f in order:
        if f.key in rejected:
            continue
        device = f.device
        auto = autos.get(device.id)
        if f.parent is not None and f.parent.id != device.parent_device_id:
            fresh = auto is None and device.parent_device_id is None
            own = auto is not None and device.parent_device_id == auto.parent_device_id
            if (fresh or own) and hierarchy.can_parent(parents, device.id, f.parent.id):
                device.parent_device_id = parents[device.id] = f.parent.id
                if auto is None:
                    auto = autos[device.id] = MapAuto(device_id=device.id)
                    session.add(auto)
                auto.parent_device_id, auto.placed_at = f.parent.id, utcnow()
                if fresh:
                    done.placed += 1
                else:
                    done.moved += 1
        if f.role is not None and device.role == DeviceRole.UNKNOWN:
            device.role = f.role
            done.roles += 1
    await session.flush()
    if done.placed or done.moved:
        log.info("Automatic map: placed %d, moved %d", done.placed, done.moved)
    return done


async def placed_automatically(session: AsyncSession, device: Device) -> bool:
    """Is this device's current place automatic's (not a person's)?"""
    auto = await session.get(MapAuto, device.id)
    return (auto is not None and device.parent_device_id is not None
            and device.parent_device_id == auto.parent_device_id)


async def take_back(session: AsyncSession, f: Found) -> bool:
    """"Not right" on a place automatic set: note the answer and take the
    device back off, so the map does not keep a place a person rejected."""
    device = f.device
    if not await placed_automatically(session, device):
        return False
    if f.parent is None or device.parent_device_id != f.parent.id:
        return False
    await dismiss(session, f)
    device.parent_device_id = None
    await session.execute(delete(MapAuto).where(MapAuto.device_id == device.id))
    await session.flush()
    return True


@dataclass
class WipeCounts:
    roles: int
    parents: int
    dismissed: int
    # The gateway as SPARK knows it now, and whether it would still know it
    # once roles are cleared (by its own addresses over SNMP). If not, a wiped
    # map has no top until someone sets the gateway's role again.
    gateway: Device | None = None
    gateway_found_without_role: bool = False


async def wipe_counts(session: AsyncSession) -> WipeCounts:
    devices = {d.id: d for d in (await session.execute(select(Device))).scalars()}
    by_addresses = await _root_by_addresses(session, devices)
    return WipeCounts(
        roles=await session.scalar(select(func.count(Device.id)).where(
            Device.role != DeviceRole.UNKNOWN)) or 0,
        parents=await session.scalar(select(func.count(Device.id)).where(
            Device.parent_device_id.is_not(None))) or 0,
        dismissed=len(await dismissed(session)),
        gateway=await _root(session, devices, set()),
        gateway_found_without_role=by_addresses is not None,
    )


async def wipe(session: AsyncSession) -> tuple[WipeCounts, Applied]:
    """Start the map over: every role and parent, and every "Not right".

    Devices, targets, services, alerts and history are untouched. In
    automatic mode the map is rebuilt at once from what SNMP last reported.
    """
    counts = await wipe_counts(session)
    await session.execute(update(Device).values(role=DeviceRole.UNKNOWN, parent_device_id=None))
    await session.execute(delete(MapAuto))
    await save_setting(session, DISMISSED, {"pairs": []})
    await session.flush()
    session.expire_all()
    return counts, await apply_automatic(session)
