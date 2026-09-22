"""The scheduled sweep.

Mirrors engine/runner.py: owns its session, contains its own failures, and
publishes only after the transaction has committed.

It also records what the sweep did, so an empty Devices page can say why it is
empty. "Swept 254 addresses, none answered" and "ICMP unavailable" and "never
ran" are three different problems, and an empty list looks identical for all
three.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from .. import events
from ..db import get_setting, save_setting, session_scope
from ..models import Device, utcnow
from ..subnets import enabled_subnets
from .ports import (
    WELL_KNOWN,
    find_interception,
    pick_control_addresses,
    scan_hosts,
)
from .services import record_all as record_services
from .store import record_all
from .sweep import SweepReport, hosts_in, sweep_all

log = logging.getLogger(__name__)

JOB_ID = "discovery:sweep"


@dataclass(frozen=True)
class _SubnetPlan:
    """What the sweep needs to know about a subnet, with no session attached.

    A sweep of several /24s takes seconds. Passing live ORM objects into it
    would keep a database connection open for the whole thing, and SQLite has
    one writer.
    """

    cidr: str
    label: str
    attached: bool
    enabled: bool = True


# Stored in the settings table rather than a table of its own: it is one small
# JSON blob, and a diagnostic is a poor reason to make this project run its
# first schema migration.
STATE_KEY = "discovery_state"

# The sweep intervals the UI offers, in minutes, and the only values accepted
# back from it. A free-number box invites "1", which on a /24 means a sweep
# still running when the next one starts; a fixed list makes the bad answers
# unavailable rather than merely discouraged.
SWEEP_INTERVAL_CHOICES: tuple[int, ...] = (5, 10, 15, 30, 60, 120, 360, 720, 1440)


async def last_sweep(session) -> dict:  # type: ignore[no-untyped-def]
    """What the previous sweep did, or {} if none has run."""
    return await get_setting(session, STATE_KEY)


async def run_sweep(config) -> None:  # type: ignore[no-untyped-def]
    """Sweep every configured subnet and record what answered."""
    new_names: list[str] = []
    report: SweepReport | None = None

    try:
        async with session_scope() as session:
            settings = await get_setting(session, "discovery")
            if not settings.get("enabled", True):
                log.debug("Discovery is disabled in settings; skipping sweep")
                return

        # Subnets come from the database, not from config.network.subnets:
        # spark.yaml seeds them once and is not read again.
        async with session_scope() as session:
            subnets = await enabled_subnets(session)
            # Detached from the session before the sweep, which takes seconds
            # and must not hold a database connection open while it runs.
            plan = [
                _SubnetPlan(cidr=s.cidr, label=s.label, attached=s.attached)
                for s in subnets
            ]

        report = await sweep_all(plan)

        async with session_scope() as session:
            seen, new = await record_all(session, report.observations)
            # Read the names inside the session, before it closes.
            new_names = [device.display_name for device in new]
            summary = report.as_dict()
            summary["new"] = len(new_names)
            await save_setting(session, STATE_KEY, summary)

        log.info(
            "Discovery: probed %d, %d answered, %d new",
            report.probed, report.answered, len(new_names),
        )
    except Exception as exc:  # noqa: BLE001 - a failed sweep must not stop the scheduler
        log.exception("Discovery sweep failed")
        # Record the failure too. A sweep that crashes silently is the worst
        # of the three cases, because the page looks the same as never having
        # run and nothing suggests looking at the logs.
        try:
            async with session_scope() as session:
                summary = (report.as_dict() if report else SweepReport().as_dict())
                summary["error"] = f"{type(exc).__name__}: {exc}"
                summary["new"] = 0
                await save_setting(session, STATE_KEY, summary)
        except Exception:  # noqa: BLE001
            log.debug("Could not record the failed sweep", exc_info=True)
        return

    if new_names:
        for name in new_names:
            log.info("New device on the network: %s", name)
    events.publish({"kind": "devices", "new": len(new_names)})


PORT_SCAN_JOB_ID = "discovery:ports"
PORT_SCAN_STATE_KEY = "port_scan_state"

# Offered in the UI and the only values accepted back. Hours, because a port
# scan is not a liveness check -- what is listening on a box changes when you
# deploy something, not minute to minute.
PORT_SCAN_INTERVAL_CHOICES: tuple[int, ...] = (1, 3, 6, 12, 24, 72, 168)


async def last_port_scan(session) -> dict:  # type: ignore[no-untyped-def]
    return await get_setting(session, PORT_SCAN_STATE_KEY)


async def run_port_scan(_config=None) -> None:  # type: ignore[no-untyped-def]
    """Scan the devices discovery has already found for listening services.

    Deliberately driven from the device table rather than from the subnets: the
    sweep decides who exists, this decides what they are running. Scanning
    every address in a /24 on the chance something answers is how a two-minute
    job becomes an hour.
    """
    summary: dict = {"started_at": utcnow().isoformat()}
    try:
        async with session_scope() as session:
            settings = await get_setting(session, "discovery")
            if not settings.get("port_scan_enabled", True):
                log.debug("Port scanning is disabled in settings; skipping")
                return

            # Only devices seen recently. A device that has not answered in a
            # fortnight is not worth a timeout per port.
            cutoff = utcnow() - timedelta(days=14)
            rows = list(
                (
                    await session.execute(
                        select(Device.id, Device.primary_ip).where(
                            Device.ignored.is_(False),
                            Device.primary_ip.isnot(None),
                            Device.last_seen.isnot(None),
                            Device.last_seen >= cutoff,
                        )
                    )
                ).all()
            )
            targets = [(int(device_id), str(ip)) for device_id, ip in rows]

            # Control addresses come from the same session: addresses in an
            # enabled subnet that discovery has never found anything at.
            known = {ip for _, ip in targets}
            all_ips = {
                str(ip) for ip in (
                    await session.execute(
                        select(Device.primary_ip).where(Device.primary_ip.isnot(None))
                    )
                ).scalars().all()
            }
            subnets = await enabled_subnets(session)
            controls: list[str] = []
            for subnet in subnets:
                if len(controls) >= 3:
                    break
                controls.extend(
                    pick_control_addresses(hosts_in(subnet.cidr), known | all_ips)
                )
            controls = controls[:3]

        if not targets:
            summary["devices"] = 0
            summary["finished_at"] = utcnow().isoformat()
            async with session_scope() as session:
                await save_setting(session, PORT_SCAN_STATE_KEY, summary)
            log.info("Port scan: no devices to scan")
            return

        # Anything that answers where nothing exists is the network talking,
        # not a service. Excluded from the scan rather than recorded.
        intercepted = await find_interception(controls)
        ports = [p for p in sorted(WELL_KNOWN) if p not in intercepted]

        scans = await scan_hosts(targets, ports)

        async with session_scope() as session:
            seen, new = await record_services(session, scans)
            summary.update({
                "devices": len(scans),
                "services": seen,
                "new": new,
                "ports_per_device": len(ports),
                "controls": len(controls),
                "intercepted": sorted(intercepted),
                "finished_at": utcnow().isoformat(),
            })
            await save_setting(session, PORT_SCAN_STATE_KEY, summary)

        log.info(
            "Port scan: %d device(s), %d service(s) answering, %d new%s",
            len(scans), seen, new,
            f" ({len(intercepted)} port(s) excluded as intercepted)" if intercepted else "",
        )
    except Exception as exc:  # noqa: BLE001 - a failed scan must not stop the scheduler
        log.exception("Port scan failed")
        summary["error"] = f"{type(exc).__name__}: {exc}"
        try:
            async with session_scope() as session:
                await save_setting(session, PORT_SCAN_STATE_KEY, summary)
        except Exception:  # noqa: BLE001
            log.debug("Could not record the failed port scan", exc_info=True)
        return

    events.publish({"kind": "services", "new": summary.get("new", 0)})
