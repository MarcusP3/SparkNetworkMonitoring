"""First-run setup after the account: subnets, SNMP, integrations, alerts.

Creating the administrator used to land on an empty dashboard, with the
next steps scattered over four Settings pages. Now `/setup` hands over to
`/welcome` (web/routes_welcome.py), which asks for them in order, the way an
installer does. Every step can be skipped and everything it saves is the
same row Settings would have made: the wizard is a front door, not a second
copy of the settings.

Only a fresh install starts it -- the setup form activates it -- so an
upgrade never meets it. `/welcome` stays reachable by hand afterwards.

Three things live here rather than in the routes:

* **The host's own subnets** (`host_subnets`), read from its interfaces, to
  pre-fill the first step. The container runs with host networking, so its
  interfaces are the host's.
* **Linking a credential to its device after the sweep** (`after_sweep`).
  A credential normally belongs to a device, and on a fresh install there
  are none yet; the wizard saves the address, and the first sweep that finds
  exactly one device there gives it that device. Only credentials the
  wizard made are linked: one left without a device in Settings stays as
  its owner left it.
* **Banners** (`banners`): the open items shown at the top of every page --
  setup not finished, an integration chosen but never given a key, and an
  integration that is not connected. Dismissing one hides it until it
  changes: a credential that recovers and then fails again is news again.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import struct
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import credentials
from .db import get_setting, save_setting
from .models import ApiCredential, Device, DeviceAddress

log = logging.getLogger(__name__)

SETTING = "onboarding"
BANNERS = "banners"

# In order. Each is a page of /welcome.
STEPS = ("subnets", "snmp", "integrations", "alerts", "finish")
TITLES = {
    "subnets": "Subnets",
    "snmp": "SNMP",
    "integrations": "Integrations",
    "alerts": "Alerts",
    "finish": "Finish",
}

# Marks a credential the wizard made, until the sweep gives it a device.
LINK_FLAG = "link_on_sweep"


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


async def load(session: AsyncSession) -> dict:
    return await get_setting(session, SETTING)


async def save(session: AsyncSession, state: dict) -> None:
    await save_setting(session, SETTING, state)


async def start(session: AsyncSession) -> None:
    """Called by the setup form, and only there: a fresh install."""
    await save(session, {"active": True, "done": False, "steps": {}, "chosen": []})


async def mark(session: AsyncSession, step: str, how: str) -> dict:
    """Record a step as "done" or "skipped"."""
    state = await load(session)
    steps = dict(state.get("steps") or {})
    steps[step] = how
    state["steps"] = steps
    await save(session, state)
    return state


def next_step(state: dict, after: str | None = None) -> str:
    """The step after `after`; or, with none, the first not yet seen."""
    if after is not None:
        index = STEPS.index(after) + 1
        return STEPS[min(index, len(STEPS) - 1)]
    seen = state.get("steps") or {}
    for step in STEPS:
        if step not in seen:
            return step
    return STEPS[-1]


async def finish(session: AsyncSession, *, find_snmp: bool) -> None:
    state = await load(session)
    state["done"] = True
    state.setdefault("steps", {})["finish"] = "done"
    state["find_snmp_after_sweep"] = bool(find_snmp)
    await save(session, state)


async def set_chosen(session: AsyncSession, kinds: list[str]) -> None:
    """The integrations ticked in the wizard, whether or not given a key."""
    state = await load(session)
    state["chosen"] = sorted({k for k in kinds if k in credentials.KINDS})
    await save(session, state)


# --------------------------------------------------------------------------
# The host's own subnets
# --------------------------------------------------------------------------

# Not networks anyone means to sweep: container and VM plumbing, VPN overlays.
_SKIP_NAMES = ("lo", "docker", "br-", "veth", "virbr", "cni", "flannel", "cali",
               "vxlan", "tailscale", "wg", "tun", "tap", "zt", "lxcbr", "podman", "kube")
_SKIP_NETS = (ipaddress.ip_network("127.0.0.0/8"),
              ipaddress.ip_network("169.254.0.0/16"),
              ipaddress.ip_network("100.64.0.0/10"))   # CGNAT; Tailscale lives here
_VLAN_IN_NAME = re.compile(r"(?:\.|vlan)(\d{1,4})$", re.IGNORECASE)

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B


@dataclass(frozen=True)
class HostSubnet:
    cidr: str
    interface: str
    address: str
    vlan: int | None
    too_large: bool


def _read_interfaces() -> list[tuple[str, str, str]]:
    """(name, address, netmask) for each interface with an IPv4 address.

    The standard library's ioctl, not a package: one address per interface
    (the primary), which is the one a person would name. Anything that goes
    wrong -- not Linux, no permission -- is an empty list: the form is then
    blank, which is what it was before.
    """
    try:
        import fcntl
    except ImportError:          # not Linux
        return []
    found = []
    try:
        names = [name for _, name in socket.if_nameindex()]
    except OSError:
        return []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for name in names:
            request = struct.pack("256s", name.encode()[:15])
            try:
                address = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), SIOCGIFADDR, request)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), SIOCGIFNETMASK, request)[20:24])
            except OSError:      # no IPv4 address on this one
                continue
            found.append((name, address, mask))
    return found


def host_subnets(interfaces: list[tuple[str, str, str]] | None = None) -> list[HostSubnet]:
    """The networks this machine has an address on, worth offering to sweep."""
    from .subnets import too_large_to_sweep

    rows = _read_interfaces() if interfaces is None else interfaces
    out: dict[str, HostSubnet] = {}
    for name, address, mask in rows:
        if name.lower().startswith(_SKIP_NAMES):
            continue
        try:
            network = ipaddress.ip_network(f"{address}/{mask}", strict=False)
        except ValueError:
            continue
        if network.prefixlen >= 31 or any(network.subnet_of(skip) for skip in _SKIP_NETS
                                          if skip.version == network.version):
            continue
        cidr = str(network)
        if cidr in out:
            continue
        match = _VLAN_IN_NAME.search(name)
        vlan = int(match.group(1)) if match and int(match.group(1)) <= 4094 else None
        out[cidr] = HostSubnet(cidr=cidr, interface=name, address=address, vlan=vlan,
                               too_large=too_large_to_sweep(cidr))
    return sorted(out.values(), key=lambda s: int(ipaddress.ip_network(s.cidr).network_address))


# --------------------------------------------------------------------------
# After a sweep
# --------------------------------------------------------------------------


def _bare_host(host: str) -> str:
    """The address or name in a credential's host, without its port."""
    host = (host or "").strip()
    if host.startswith("["):
        return host[1:].partition("]")[0]
    return host.partition(":")[0] if host.count(":") == 1 else host


async def _device_at(session: AsyncSession, host: str) -> int | None:
    """The one device at this address or host name, or None (none, or two)."""
    bare = _bare_host(host)
    if not bare:
        return None
    ids: set[int] = set()
    ids.update((await session.execute(select(Device.id).where(
        Device.primary_ip == bare, Device.ignored.is_(False)))).scalars())
    ids.update((await session.execute(select(DeviceAddress.device_id).join(
        Device, Device.id == DeviceAddress.device_id).where(
        DeviceAddress.ip == bare, Device.ignored.is_(False)))).scalars())
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        short = bare.lower().split(".")[0]
        ids.update((await session.execute(select(Device.id).where(
            Device.ignored.is_(False),
            (func.lower(Device.hostname) == bare.lower())
            | (func.lower(Device.hostname) == short)))).scalars())
    return ids.pop() if len(ids) == 1 else None


async def link_credentials(session: AsyncSession) -> int:
    """Give each credential the wizard made the device at its address."""
    rows = (await session.execute(select(ApiCredential).where(
        ApiCredential.device_id.is_(None)))).scalars().all()
    linked = 0
    for row in rows:
        options = dict(row.options or {})
        if not options.get(LINK_FLAG):
            continue
        device_id = await _device_at(session, row.host)
        if device_id is None:
            continue
        row.device_id = device_id
        options.pop(LINK_FLAG, None)
        row.options = options
        linked += 1
        log.info("Credential %r is on device %d, found at %s", row.name, device_id, row.host)
    return linked


async def after_sweep(session: AsyncSession) -> bool:
    """Run in the sweep's own transaction. True if the wizard asked for
    Find SNMP once the first scan had found something to ask -- once."""
    await link_credentials(session)
    state = await load(session)
    if not state.pop("find_snmp_after_sweep", False):
        return False
    await save(session, state)
    return True


# --------------------------------------------------------------------------
# Banners
# --------------------------------------------------------------------------


def _short(text: str | None, limit: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def open_items(session: AsyncSession) -> list[dict]:
    """Every open item, dismissed or not: key, sig, tone, text, link."""
    state = await load(session)
    items: list[dict] = []
    if state.get("active") and not state.get("done"):
        items.append({"key": "setup", "sig": "1", "tone": "info",
                      "text": "SPARK is not fully set up yet: subnets, SNMP, integrations and alerts.",
                      "link": "/welcome", "link_text": "Continue setup"})
    rows = (await session.execute(select(ApiCredential).order_by(ApiCredential.name))).scalars().all()
    have = {row.kind for row in rows}
    for kind in state.get("chosen") or []:
        if kind in have or kind not in credentials.KINDS:
            continue
        label = credentials.KINDS[kind]
        items.append({"key": f"chosen:{kind}", "sig": "1", "tone": "info",
                      "text": f"You chose {label} during setup but it has no API key yet.",
                      "link": f"/settings/credentials?add={kind}#credentials",
                      "link_text": f"Add {label}"})
    for row in rows:
        if not row.enabled or credentials.state(row) != "bad":
            continue
        label = credentials.KINDS.get(row.kind, row.kind)
        # Changes when it next works: a failure after a recovery is new.
        sig = row.last_ok_at.isoformat() if row.last_ok_at else "never"
        reason = _short(row.last_error)
        items.append({"key": f"cred:{row.id}", "sig": sig, "tone": "warn",
                      "text": f"{label} {row.name!r} is not connected" + (f": {reason}" if reason else "."),
                      "link": f"/settings/credentials#cred-{row.id}",
                      "link_text": "Open"})
    return items


async def banners(session: AsyncSession) -> list[dict]:
    """The open items not dismissed in their present state."""
    dismissed = (await get_setting(session, BANNERS)).get("dismissed") or {}
    return [item for item in await open_items(session) if dismissed.get(item["key"]) != item["sig"]]


async def dismiss(session: AsyncSession, key: str, sig: str) -> bool:
    """Hide one item in its present state. Only an item that is open, with
    the signature it has now, can be dismissed; entries for items that have
    gone are dropped at the same time, so the setting stays small."""
    current = {item["key"]: item["sig"] for item in await open_items(session)}
    if current.get(key) != sig:
        return False
    setting = await get_setting(session, BANNERS)
    dismissed = {k: v for k, v in (setting.get("dismissed") or {}).items() if k in current}
    dismissed[key] = sig
    await save_setting(session, BANNERS, {"dismissed": dismissed})
    return True
