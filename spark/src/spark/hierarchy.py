"""Where a device sits: its declared parent, and the chain above it.

Set by hand on each device's page ("Connected to"), and used twice: the
service map draws it, and a failure is not alerted while anything above it
is down -- the switch goes, you hear about the switch, not the twenty hosts
behind it (engine/state.py, alerts.py).

Cycles cannot be saved (`can_parent`), but the walks below still stop at a
repeat and at a depth limit: a database edited by hand should give a wrong
answer, not a hung request.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Device, HealthStatus, Target

MAX_DEPTH = 32


async def ancestors(session: AsyncSession, device_id: int) -> list[int]:
    """The devices above this one, nearest first."""
    chain: list[int] = []
    seen = {device_id}
    current = device_id
    for _ in range(MAX_DEPTH):
        parent = await session.scalar(
            select(Device.parent_device_id).where(Device.id == current)
        )
        if parent is None or parent in seen:
            break
        chain.append(parent)
        seen.add(parent)
        current = parent
    return chain


async def upstream_is_down(session: AsyncSession, device_id: int | None) -> bool:
    """Is a device above this one down, as its own targets report it."""
    if device_id is None:
        return False
    above = await ancestors(session, device_id)
    if not above:
        return False
    down = await session.scalar(
        select(Target.id).where(
            Target.device_id.in_(above),
            Target.enabled.is_(True),
            Target.status == HealthStatus.DOWN,
        ).limit(1)
    )
    return down is not None


def descendants(parents: dict[int, int | None], device_id: int) -> set[int]:
    """Every device below this one, from an id -> parent id map."""
    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(child)
    found: set[int] = set()
    stack = [device_id]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found and child != device_id:
                found.add(child)
                stack.append(child)
    return found


def can_parent(parents: dict[int, int | None], device_id: int, parent_id: int) -> bool:
    """Whether `parent_id` may be set as this device's parent: not itself,
    and not anything below it, which would make a loop."""
    return parent_id != device_id and parent_id not in descendants(parents, device_id)
