"""Which ports the scan looks at, and what they are called.

`discovery/ports.py` ships an opinionated list of 45. It is a good default and
a bad answer to "what is on *my* network" -- a homelab runs Deluge on 8112 and
Node-RED on 1880 and neither is in anybody's well-known list, while four
Windows ports are pure cost on a network with no Windows on it.

So the list is editable in both directions, and that is the point. Every port
added costs scan time and every port removed buys it back, which is why the
Settings page states the budget in seconds rather than leaving it to be
discovered later as "the scan got slow".

Kept in the settings blob rather than a table of its own. It is configuration,
it has no relationships, the port number is its own natural key, and this
project has twice taken its own startup down with an `ALTER TABLE` -- a list of
at most 40 small records is not worth a sixth migration.

Nothing here scans anything; it reads and writes the catalogue and hands out
the merged result, the same split that keeps `ports.py` testable without a
network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .db import get_setting, save_setting
from .discovery.ports import PER_HOST_CONCURRENCY, TOTAL_CONCURRENCY, WELL_KNOWN

log = logging.getLogger(__name__)

SETTING_KEY = "port_catalogue"

# A ceiling on the custom list, not on the scan. It is the difference between
# "I added the ports I care about" and "I pasted a range in", and the second
# one is how a twelve-second scan becomes a four-minute one without anybody
# deciding that it should.
MAX_CUSTOM = 40

# Names are documentation -- they land in a table cell on the Devices page, and
# a long one wrecks the column for every row.
MAX_NAME = 32

MIN_PORT = 1
MAX_PORT = 65535


@dataclass(frozen=True)
class CustomPort:
    port: int
    name: str | None = None

    def as_dict(self) -> dict:
        return {"port": self.port, "name": self.name}


@dataclass(frozen=True)
class Catalogue:
    custom: tuple[CustomPort, ...] = ()
    # Built-ins switched off. Stored as the ports themselves rather than as
    # "the ones you kept": a later release adding a port to WELL_KNOWN should
    # start scanning it, not have it silently excluded because it was not in a
    # list written before it existed.
    disabled: frozenset[int] = field(default_factory=frozenset)

    def ports(self) -> dict[int, str | None]:
        """The effective catalogue: port -> name, in the order it is scanned.

        Built-ins first, minus the ones switched off, then the custom entries.
        A custom entry on a built-in's port therefore *renames* it, which is
        deliberate: 3000 is Grafana to most people and someone else's app to
        you, and the name is only ever documentation.
        """
        merged: dict[int, str | None] = {
            port: name for port, name in WELL_KNOWN.items() if port not in self.disabled
        }
        for entry in self.custom:
            merged[entry.port] = entry.name
        return dict(sorted(merged.items()))

    def as_dict(self) -> dict:
        return {
            "custom": [entry.as_dict() for entry in self.custom],
            "disabled": sorted(self.disabled),
        }


def from_dict(raw: dict | None) -> Catalogue:
    """Rebuild a catalogue from stored JSON, discarding anything unusable.

    Forgiving on the way in on purpose. This blob is hand-editable in the
    database and survives upgrades that may change what a valid entry looks
    like; one bad record should cost its own line, not the Settings page.
    """
    if not isinstance(raw, dict):
        return Catalogue()

    custom: list[CustomPort] = []
    seen: set[int] = set()
    for item in raw.get("custom") or []:
        if not isinstance(item, dict):
            continue
        port = parse_port(item.get("port"))
        if port is None or port in seen:
            continue
        seen.add(port)
        custom.append(CustomPort(port=port, name=clean_name(item.get("name"))))

    disabled = set()
    for item in raw.get("disabled") or []:
        port = parse_port(item)
        # Only ever a built-in. A stale entry for a port that is no longer in
        # WELL_KNOWN would otherwise sit there suppressing nothing, and would
        # switch a port off by surprise if that number were ever added back.
        if port is not None and port in WELL_KNOWN:
            disabled.add(port)

    return Catalogue(custom=tuple(custom), disabled=frozenset(disabled))


def parse_port(value) -> int | None:  # type: ignore[no-untyped-def]
    """A port number, or None. Accepts what a person types, not what JSON holds.

    Through `str()` rather than `int()` directly, and that is what keeps `True`
    out: bool is an int in Python, so `int(True)` is port 1 while `int("True")`
    raises. An explicit isinstance guard stood here until a mutation check
    showed it could be deleted with nothing failing -- it was dead code, having
    been handled by this line all along.
    """
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if MIN_PORT <= port <= MAX_PORT else None


def clean_name(value) -> str | None:  # type: ignore[no-untyped-def]
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())[:MAX_NAME].strip()
    return name or None


# --------------------------------------------------------------------------
# What it costs
# --------------------------------------------------------------------------


def worst_case_seconds(port_count: int, device_count: int, timeout: float = 1.0) -> int:
    """How long a scan can take when nothing answers, in seconds.

    The case worth quoting. An open port and a closed port both resolve
    immediately; a *filtered* one -- a firewall dropping rather than rejecting
    -- costs the full timeout, and a network where that is the norm is the one
    where somebody notices the scan.

    Two limits, whichever binds:

      * one host can have `PER_HOST_CONCURRENCY` probes in flight, so a single
        device takes ceil(ports / 12) timeouts however few devices there are;
      * the scan as a whole is capped at `TOTAL_CONCURRENCY`, which is what
        binds once there are enough devices to saturate it.

    Measured against black-holed addresses before this was written, because the
    comment this replaced had the same shape and no concurrency term in it, and
    was out by roughly tenfold as a result.
    """
    if port_count <= 0 or device_count <= 0:
        return 0
    per_host = -(-port_count // PER_HOST_CONCURRENCY)
    overall = -(-(port_count * device_count) // TOTAL_CONCURRENCY)
    return int(max(per_host, overall) * timeout)


# --------------------------------------------------------------------------
# Reading and writing
# --------------------------------------------------------------------------


async def load(session) -> Catalogue:  # type: ignore[no-untyped-def]
    return from_dict(await get_setting(session, SETTING_KEY))


async def save(session, catalogue: Catalogue) -> None:  # type: ignore[no-untyped-def]
    await save_setting(session, SETTING_KEY, catalogue.as_dict())


async def add_custom(session, port_value, name_value) -> str | None:  # type: ignore[no-untyped-def]
    """Add one custom port. Returns an error for the page, or None on success.

    An error string rather than an exception: every one of these is something a
    person typed, and the page has to put the message next to the field with
    the typing still in it.
    """
    port = parse_port(port_value)
    if port is None:
        typed = str(port_value).strip()
        return (f"{typed!r} is not a port number." if typed
                else "Enter a port number.")

    catalogue = await load(session)
    if any(entry.port == port for entry in catalogue.custom):
        return f"Port {port} is already on the list."
    if len(catalogue.custom) >= MAX_CUSTOM:
        return (f"That is {MAX_CUSTOM} custom ports, which is the limit. "
                "Remove one first, or switch off some built-ins you do not need.")

    name = clean_name(name_value)
    await save(session, Catalogue(
        custom=catalogue.custom + (CustomPort(port=port, name=name),),
        disabled=catalogue.disabled,
    ))
    return None


async def remove_custom(session, port_value) -> None:  # type: ignore[no-untyped-def]
    port = parse_port(port_value)
    if port is None:
        return
    catalogue = await load(session)
    await save(session, Catalogue(
        custom=tuple(e for e in catalogue.custom if e.port != port),
        disabled=catalogue.disabled,
    ))


async def set_builtins(session, keep: list) -> None:  # type: ignore[no-untyped-def]
    """Record which built-ins stay on, from the ticked boxes of the whole form.

    Takes the ones to *keep* because that is what a form of checkboxes submits
    -- an unticked box sends nothing at all. Storing the complement is the
    other half of the reason `disabled` holds the ports switched off rather
    than the ports kept.
    """
    wanted = {p for p in (parse_port(value) for value in keep) if p in WELL_KNOWN}
    catalogue = await load(session)
    await save(session, Catalogue(
        custom=catalogue.custom,
        disabled=frozenset(WELL_KNOWN) - wanted,
    ))
