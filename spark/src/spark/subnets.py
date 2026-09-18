"""Managing the network segments SPARK discovers on.

Subnets used to live in `spark.yaml`, which meant adding one was an SSH
session, a file edit and a container restart. They live in the database now.
The YAML entries seed the table once, on the first start after upgrading, and
are ignored from then on — see `seed_from_config` for why that is a one-shot
rather than a merge on every boot.

Validation is the substance of this module. A subnet with a typo in its CIDR
does not raise anywhere useful: it simply never matches a device, and the
Devices page quietly shows one fewer segment than you configured. So the CIDR
is parsed and canonicalised on the way in, not on the way out.
"""

from __future__ import annotations

import ipaddress
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_setting, save_setting
from .models import Subnet

log = logging.getLogger(__name__)

SETTINGS_KEY = "network"
MAX_VLAN = 4094


class SubnetError(ValueError):
    """Something the user can fix, phrased for them rather than for a log."""


def normalise_cidr(raw: str) -> str:
    """Parse a CIDR and return its canonical form.

    `strict=False` so that typing the address of the box you are standing on —
    "10.1.10.7/24" — is accepted and stored as "10.1.10.0/24". That is what
    people actually type, and rejecting it teaches them to distrust the field
    rather than to read the docs.
    """
    text = (raw or "").strip()
    if not text:
        raise SubnetError("A CIDR is required, for example 10.1.10.0/24.")
    if "/" not in text:
        raise SubnetError(
            f"{text!r} has no prefix length. Add one, for example {text}/24."
        )
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise SubnetError(f"{text!r} is not a valid network: {exc}") from exc
    return str(network)


def parse_vlan(raw: str | int | None) -> int | None:
    """A VLAN tag, or None. Blank is a legitimate answer, not an error."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        vlan = int(text)
    except ValueError as exc:
        raise SubnetError(f"{text!r} is not a VLAN number.") from exc
    if not 0 <= vlan <= MAX_VLAN:
        raise SubnetError(f"VLAN {vlan} is out of range — tags run 0 to {MAX_VLAN}.")
    return vlan


def too_large_to_sweep(cidr: str) -> bool:
    """Whether the sweep will refuse this subnet as too big.

    Not a validation error: a /16 is a perfectly reasonable thing to record for
    documentation. It is a warning, because the alternative is a subnet that
    looks configured and silently never gets swept.
    """
    # Imported here rather than at module scope: spark.discovery's package
    # __init__ pulls in runner, which imports this module, so a top-level
    # import would close the loop.
    from .discovery.sweep import MAX_HOSTS_PER_SUBNET

    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    return max(0, network.num_addresses - 2) > MAX_HOSTS_PER_SUBNET


async def list_subnets(session: AsyncSession) -> list[Subnet]:
    """Every subnet, ordered the way a person would write them down."""
    rows = list((await session.execute(select(Subnet))).scalars().all())
    # Sorted by address rather than by name or id, so the list reads like a
    # network diagram instead of like an insertion log.
    def key(subnet: Subnet):  # type: ignore[no-untyped-def]
        network = subnet.network
        return (0, int(network.network_address)) if network else (1, 0)

    return sorted(rows, key=key)


async def enabled_subnets(session: AsyncSession) -> list[Subnet]:
    return [s for s in await list_subnets(session) if s.enabled]


async def create(
    session: AsyncSession,
    *,
    cidr: str,
    name: str = "",
    vlan: str | int | None = None,
    attached: bool = True,
    enabled: bool = True,
    notes: str = "",
) -> Subnet:
    canonical = normalise_cidr(cidr)
    clash = await session.scalar(select(Subnet).where(Subnet.cidr == canonical))
    if clash is not None:
        raise SubnetError(f"{canonical} is already configured as {clash.label!r}.")

    subnet = Subnet(
        cidr=canonical,
        name=(name or "").strip() or None,
        vlan=parse_vlan(vlan),
        attached=attached,
        enabled=enabled,
        notes=(notes or "").strip() or None,
    )
    session.add(subnet)
    await session.flush()
    return subnet


async def update(
    session: AsyncSession,
    subnet_id: int,
    *,
    cidr: str,
    name: str = "",
    vlan: str | int | None = None,
    attached: bool = True,
    enabled: bool = True,
    notes: str = "",
) -> Subnet:
    subnet = await session.get(Subnet, subnet_id)
    if subnet is None:
        raise SubnetError("That subnet no longer exists.")

    canonical = normalise_cidr(cidr)
    clash = await session.scalar(
        select(Subnet).where(Subnet.cidr == canonical, Subnet.id != subnet_id)
    )
    if clash is not None:
        raise SubnetError(f"{canonical} is already configured as {clash.label!r}.")

    subnet.cidr = canonical
    subnet.name = (name or "").strip() or None
    subnet.vlan = parse_vlan(vlan)
    subnet.attached = attached
    subnet.enabled = enabled
    subnet.notes = (notes or "").strip() or None
    await session.flush()
    return subnet


async def delete(session: AsyncSession, subnet_id: int) -> str | None:
    """Remove a subnet. Returns its label, or None if it was already gone.

    Devices found on it are deliberately left alone. They are evidence that
    something was on the network, and deleting the segment you were looking
    through is not a statement about the things you saw. They stop matching the
    subnet filter, which is the honest outcome.
    """
    subnet = await session.get(Subnet, subnet_id)
    if subnet is None:
        return None
    label = subnet.label
    await session.delete(subnet)
    return label


async def seed_from_config(session: AsyncSession, config) -> list[str]:  # type: ignore[no-untyped-def]
    """Copy spark.yaml's subnets into the database, once ever.

    Guarded by a flag rather than by "is the table empty", because those differ
    in the case that matters: someone upgrades, deletes the subnets they no
    longer use, restarts, and finds them back. A merge on every boot would make
    the UI's delete button a lie.
    """
    state = await get_setting(session, SETTINGS_KEY)
    if state.get("subnets_seeded"):
        return []

    seeded: list[str] = []
    for entry in getattr(config.network, "subnets", []):
        try:
            canonical = normalise_cidr(entry.cidr)
        except SubnetError:
            log.warning("Skipping unparseable subnet in spark.yaml: %r", entry.cidr)
            continue
        exists = await session.scalar(select(Subnet).where(Subnet.cidr == canonical))
        if exists is not None:
            continue
        session.add(
            Subnet(
                cidr=canonical,
                name=(entry.name or "").strip() or None,
                vlan=entry.vlan,
                attached=entry.attached,
                enabled=getattr(entry, "enabled", True),
            )
        )
        seeded.append(canonical)

    state["subnets_seeded"] = True
    await save_setting(session, SETTINGS_KEY, state)
    await session.flush()
    if seeded:
        log.info(
            "Seeded %d subnet(s) from spark.yaml into the database: %s. "
            "Subnets are managed in the UI from now on; the file is no longer read.",
            len(seeded), ", ".join(seeded),
        )
    return seeded


async def count_enabled(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Subnet).where(Subnet.enabled.is_(True))
        )
        or 0
    )


def subnet_for(subnets: list[Subnet], address: str | None) -> Subnet | None:
    """Which subnet an address belongs to.

    Most specific wins, so a /24 carved out of a documented /16 is the answer
    rather than whichever happened to be created first.
    """
    matches = [s for s in subnets if s.contains(address)]
    if not matches:
        return None
    return max(matches, key=lambda s: s.network.prefixlen if s.network else 0)
