"""Target management: list, create, edit, delete, and check-on-demand.

There is no discovery yet, so this is the only way targets get into the
database. That makes it load-bearing rather than a convenience: without it the
check engine has nothing to run, and "edit the database by hand over SSH" is
exactly the experience the Setting model was written to avoid.
"""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import charts, limits, proxmox_health, tls
from .. import events
from ..checks import heartbeat
from .. import scheduler as scheduler_module
from ..config import Config
# Aliased: the module-level PAUSED here is an incident resolution, and
# HealthStatus.PAUSED is a target state. Two different things, one word.
from ..engine.state import PAUSED as PAUSED_RESOLUTION
from ..engine.state import close_open_incidents
from ..models import (
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_RECOVERY_THRESHOLD,
    DEFAULT_TIMEOUT_SECONDS,
    CheckResult,
    CheckType,
    Device,
    DeviceAddress,
    HealthStatus,
    Incident,
    Target,
    User,
    utcnow,
)
from .deps import ItemId, get_config, get_session, redirect, require_user, templates

router = APIRouter()

# Sensible starting points per check type, shown as the form's placeholder so
# the params box is documentation rather than a mystery.
PARAM_HINTS: dict[str, str] = {
    "ping": '{"count": 3, "loss_warn_percent": 1}',
    "tcp": '{"port": 443}',
    "http": '{"expect_status": 200, "expect_body": "", "cert_warn_days": 14}',
    "dns": '{"rdtype": "A", "server": "", "expect": ""}',
    "hermes": '{"verify_tls": true}',
}


def _parse_params(raw: str) -> tuple[dict, str | None]:
    raw = (raw or "").strip()
    if not raw:
        return {}, None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"Params must be valid JSON ({exc.msg} at position {exc.pos})."
    if not isinstance(value, dict):
        return {}, "Params must be a JSON object, for example {\"port\": 443}."
    return value, None


# Bounds for the tuning fields. Not a policy, a sanity check: a 0-second
# timeout fails every probe, a 0-second interval polls in a busy loop, and a
# 3600-second timeout keeps one target's job -- and its slot in the scheduler
# -- open for an hour. None of these are things the form offers, so a value
# outside them did not come from it.
MIN_INTERVAL_SECONDS = 5
MAX_INTERVAL_SECONDS = 86400
MIN_TIMEOUT_SECONDS = 0.5
MAX_TIMEOUT_SECONDS = 60.0
MAX_THRESHOLD = 100


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


async def _validate(
    session: AsyncSession,
    *,
    check_type: str,
    depends_on_target_id: str,
    editing: Target | None,
    name: str = "x",
    address: str = "x",
) -> tuple[CheckType | None, int | None, str | None]:
    """The two fields a form can get wrong in a way `int()` does not catch.

    Both used to raise out of the route as a 500: `CheckType("nope")` is a
    `ValueError`, and a dependency on a target that does not exist -- or on
    itself -- was either an integrity error at commit or a target whose
    failures were forever a symptom of its own failure.
    """
    # `required` on the page stops an empty box, but not one of spaces --
    # which strip() then saved as a target with no name at all.
    if not name.strip():
        return None, None, "Give the target a name."
    if not address.strip():
        return None, None, "Enter an address to check."
    try:
        kind = CheckType(check_type)
    except ValueError:
        return None, None, f"{check_type!r} is not a check type."
    if kind is CheckType.DOCKER:
        return None, None, "Docker checks are not available yet."
    if kind is CheckType.HEARTBEAT or (editing is not None and editing.check_type == CheckType.HEARTBEAT):
        return None, None, "Heartbeats are set up with Add heartbeat on the Targets page."

    parent_id: int | None = None
    if depends_on_target_id.strip():
        parent_id = limits.as_id(depends_on_target_id)
        if parent_id is None:
            return None, None, "Choose a target from the list, or none."
        if editing is not None and parent_id == editing.id:
            return None, None, "A target cannot depend on itself."
        if await session.get(Target, parent_id) is None:
            return None, None, "That target no longer exists."
    return kind, parent_id, None


# A Hermes dashboard's status gathers a good deal each time it is asked
# (its own code calls it heavyweight): once a minute is plenty.
INTERVALS = {CheckType.HERMES: 60}


def default_interval(kind: CheckType | str | None) -> int:
    return INTERVALS.get(kind, DEFAULT_INTERVAL_SECONDS)  # type: ignore[arg-type]


def _is_tuned(target: Target | None) -> bool:
    """Does this target differ from the defaults.

    Decides whether the tuning section starts open when editing. It has to:
    the fields are disabled while the box is unticked, disabled fields are not
    submitted, and the server then falls back to the defaults -- so a target
    with a 300s interval edited with the box closed would silently be reset to
    15s. Opening it for anything non-default keeps the values visible and
    intentional.
    """
    if target is None:
        return False
    return (
        target.interval_seconds != default_interval(target.check_type)
        or float(target.timeout_seconds) != DEFAULT_TIMEOUT_SECONDS
        or target.failure_threshold != DEFAULT_FAILURE_THRESHOLD
        or target.recovery_threshold != DEFAULT_RECOVERY_THRESHOLD
    )


async def _form_context(
    session: AsyncSession, config: Config, user: User, target: Target | None, **extra
) -> dict:
    # Nothing depends on a heartbeat: a late backup is not a reason a host
    # cannot be reached.
    others = list(
        (
            await session.execute(
                select(Target).where(Target.id != (target.id if target else -1),
                                     Target.check_type != CheckType.HEARTBEAT)
                .order_by(Target.name)
            )
        )
        .scalars()
        .all()
    )
    return {
        "config": config,
        "user": user,
        "title": "Edit target" if target else "New target",
        "target": target,
        "check_types": [t.value for t in CheckType
                        if t not in (CheckType.DOCKER, CheckType.HEARTBEAT)],
        "param_hints": PARAM_HINTS,
        # Each type's interval when the timing box is left closed ('' for the rest).
        "intervals": {"": DEFAULT_INTERVAL_SECONDS, **{k.value: v for k, v in INTERVALS.items()}},
        "candidates": others,
        "tuned": _is_tuned(target),
        "defaults": {
            "interval_seconds": DEFAULT_INTERVAL_SECONDS,
            "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "failure_threshold": DEFAULT_FAILURE_THRESHOLD,
            "recovery_threshold": DEFAULT_RECOVERY_THRESHOLD,
        },
        **extra,
    }


def target_host(address: str) -> str:
    """The host a target's address points at: from a URL, host:port,
    [v6]:port, or a bare name or address. Lower case."""
    raw = (address or "").strip()
    if "://" in raw:
        try:
            return (urlsplit(raw).hostname or "").lower()
        except ValueError:
            return ""
    if raw.startswith("["):
        return raw[1:].partition("]")[0].lower()
    host, sep, tail = raw.rpartition(":")
    if sep and tail.isdigit() and ":" not in host:
        return host.lower()
    return raw.lower()


def ago(when: datetime | None, now: datetime) -> str:
    """How long since, in the fewest words: "12 s ago", "4 min ago"."""
    if when is None:
        return "never checked"
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def every(seconds: int) -> str:
    """A check interval as people say it: "15 s", "1 min", "2 h"."""
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


STATUS_ORDER = {"down": 0, "degraded": 1, "unknown": 2, "up": 3, "paused": 4}


async def devices_for(session: AsyncSession, targets: list[Target]) -> dict[int, Device]:
    """The device each target belongs to, for linking to its page: the one
    it was made from (Watch), or else the device at its address -- primary,
    merged, or by host name. Ignored devices are left out."""
    devices = {d.id: d for d in (await session.execute(
        select(Device).where(Device.ignored.is_(False)))).scalars()}
    by_host: dict[str, int] = {}
    for d in devices.values():
        for name in (d.hostname, d.primary_ip):
            if name:
                by_host.setdefault(name.lower(), d.id)
    for ip, device_id in (await session.execute(
            select(DeviceAddress.ip, DeviceAddress.device_id))).all():
        by_host.setdefault(ip.lower(), device_id)
    out: dict[int, Device] = {}
    for t in targets:
        if t.check_type == CheckType.HEARTBEAT:
            continue          # belongs to no device, whatever its address reads
        device_id = t.device_id if t.device_id in devices else by_host.get(target_host(t.address))
        if device_id in devices:
            out[t.id] = devices[device_id]
    return out


@router.get("/targets")
async def list_targets(
    request: Request,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    targets = list(
        (await session.execute(select(Target).order_by(Target.name))).scalars().all()
    )
    open_counts = dict(
        (
            await session.execute(
                select(Incident.target_id, func.count())
                .where(Incident.closed_at.is_(None))
                .group_by(Incident.target_id)
            )
        ).all()
    )
    # Each target's last few results, newest first: the latest is the row's
    # latency and result, the rest its trend. One small query per target, not
    # a window over the table: each is an index seek on (target_id, ts) that
    # stops after TREND_POINTS rows, where a window over the raw table would
    # sort a week of every target's results to keep thirty of each.
    now = utcnow()
    rows = []
    for t in targets:
        recent = (await session.execute(
            select(CheckResult.latency_ms, CheckResult.detail, CheckResult.suppressed)
            .where(CheckResult.target_id == t.id)
            .order_by(CheckResult.ts.desc())
            .limit(charts.TREND_POINTS)
        )).all()
        status = getattr(t.status, "value", t.status)
        latest = recent[0] if recent else None
        beat = t.check_type == CheckType.HEARTBEAT
        rows.append({
            "status": status,
            "latency_ms": latest.latency_ms if latest else None,
            "detail": (latest.detail if latest else
                       "Waiting for the first ping." if beat and t.last_ping_at is None else None),
            "ago": ago(t.last_checked_at, now),
            # A heartbeat is judged every minute; what matters is how often
            # the job is meant to run.
            "every": every(int(heartbeat.period_of(t.params or {}).total_seconds())) if beat
                     else every(t.interval_seconds),
            "heartbeat": beat,
            # Speed-test samples are left out of the trend, as on the charts:
            # a deliberately saturated link is not the target getting slower.
            "trend": charts.trend([r.latency_ms for r in reversed(recent) if not r.suppressed],
                                  status),
            "order": STATUS_ORDER.get(status, 2),
        })

    enabled = [t for t in targets if t.enabled]
    statuses = [r["status"] for r in rows]
    stats = {
        "total": len(targets),
        "up": statuses.count("up"),
        "degraded": statuses.count("degraded"),
        "down": statuses.count("down"),
        "paused": len(targets) - len(enabled),
        # What the engine does in a minute, at the intervals set: a load figure
        # rather than a health one.
        "per_minute": round(sum(60 / max(t.interval_seconds, 1) for t in enabled)),
    }
    return templates.TemplateResponse(
        request,
        "targets.html",
        {
            "config": config,
            "user": user,
            "title": "Targets",
            "targets": targets,
            "rows": rows,
            "stats": stats,
            "check_types": [k.value for k in CheckType if k is not CheckType.DOCKER],
            "open_counts": open_counts,
            # Each target's device, to link its name to the device's page.
            "device_of": await devices_for(session, targets),
            # VMs and containers watched on a Proxmox card: alerted on too,
            # so listed here with the rest of what SPARK watches.
            "pve_guests": await proxmox_health.watched_guests(session),
        },
    )


@router.get("/targets/new")
async def new_target_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    context = await _form_context(session, config, user, None)
    return templates.TemplateResponse(request, "target_form.html", context)


@router.post("/targets/new")
async def create_target(
    request: Request,
    name: str = Form(..., max_length=limits.NAME),
    check_type: str = Form(..., max_length=limits.SHORT),
    address: str = Form(..., max_length=limits.ADDRESS),
    interval_seconds: int = Form(DEFAULT_INTERVAL_SECONDS),
    timeout_seconds: float = Form(DEFAULT_TIMEOUT_SECONDS, allow_inf_nan=False),
    failure_threshold: int = Form(DEFAULT_FAILURE_THRESHOLD),
    recovery_threshold: int = Form(DEFAULT_RECOVERY_THRESHOLD),
    depends_on_target_id: str = Form("", max_length=limits.SHORT),
    params: str = Form("", max_length=limits.PARAMS),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    parsed, error = _parse_params(params)
    kind, parent_id = None, None
    if not error:
        kind, parent_id, error = await _validate(
            session, check_type=check_type, depends_on_target_id=depends_on_target_id,
            name=name, address=address,
            editing=None,
        )
    if error or kind is None:
        context = await _form_context(
            session, config, user, None, error=error, submitted=await request.form()
        )
        return templates.TemplateResponse(request, "target_form.html", context, status_code=400)
    if "interval_seconds" not in await request.form():     # the timing box left closed
        interval_seconds = default_interval(kind)

    target = Target(
        name=name.strip(),
        check_type=kind,
        address=address.strip(),
        params=parsed,
        interval_seconds=int(_clamp(int(interval_seconds), MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)),
        timeout_seconds=_clamp(float(timeout_seconds), MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS),
        failure_threshold=int(_clamp(int(failure_threshold), 1, MAX_THRESHOLD)),
        recovery_threshold=int(_clamp(int(recovery_threshold), 1, MAX_THRESHOLD)),
        depends_on_target_id=parent_id,
        status=HealthStatus.UNKNOWN,
    )
    session.add(target)
    await session.flush()
    await session.commit()

    events.publish({"kind": "created", "target_id": target.id})
    scheduler_module.schedule_target(target)
    # Check straight away, so adding a target tells you whether it works now
    # rather than at the top of the next interval.
    await scheduler_module.run_now(target.id)
    return redirect("/targets")


@router.get("/targets/{target_id}/edit")
async def edit_target_form(
    request: Request,
    target_id: ItemId,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    target = await session.get(Target, target_id)
    if target is None:
        return redirect("/targets")
    if target.check_type == CheckType.HEARTBEAT:
        return redirect(f"/targets/{target.id}/heartbeat")
    context = await _form_context(session, config, user, target)
    return templates.TemplateResponse(request, "target_form.html", context)


@router.post("/targets/{target_id}/edit")
async def update_target(
    request: Request,
    target_id: ItemId,
    name: str = Form(..., max_length=limits.NAME),
    check_type: str = Form(..., max_length=limits.SHORT),
    address: str = Form(..., max_length=limits.ADDRESS),
    interval_seconds: int = Form(DEFAULT_INTERVAL_SECONDS),
    timeout_seconds: float = Form(DEFAULT_TIMEOUT_SECONDS, allow_inf_nan=False),
    failure_threshold: int = Form(DEFAULT_FAILURE_THRESHOLD),
    recovery_threshold: int = Form(DEFAULT_RECOVERY_THRESHOLD),
    depends_on_target_id: str = Form("", max_length=limits.SHORT),
    params: str = Form("", max_length=limits.PARAMS),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    target = await session.get(Target, target_id)
    if target is None:
        return redirect("/targets")

    parsed, error = _parse_params(params)
    kind, parent_id = None, None
    if not error:
        kind, parent_id, error = await _validate(
            session, check_type=check_type, depends_on_target_id=depends_on_target_id,
            name=name, address=address,
            editing=target,
        )
    if error or kind is None:
        context = await _form_context(session, config, user, target, error=error)
        return templates.TemplateResponse(request, "target_form.html", context, status_code=400)

    if "interval_seconds" not in await request.form():     # the timing box left closed
        interval_seconds = default_interval(kind)
    if kind != target.check_type or address.strip() != target.address:
        target.readings = None        # another thing now: its card starts again
    target.name = name.strip()
    target.check_type = kind
    target.address = address.strip()
    target.params = parsed
    target.interval_seconds = int(_clamp(int(interval_seconds), MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS))
    target.timeout_seconds = _clamp(float(timeout_seconds), MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    target.failure_threshold = int(_clamp(int(failure_threshold), 1, MAX_THRESHOLD))
    target.recovery_threshold = int(_clamp(int(recovery_threshold), 1, MAX_THRESHOLD))
    target.depends_on_target_id = parent_id
    await session.commit()

    events.publish({"kind": "updated", "target_id": target.id})
    if target.enabled:
        scheduler_module.schedule_target(target)
    return redirect("/targets")


# --------------------------------------------------------------------------
# Heartbeats (checks/heartbeat.py, web/routes_heartbeat.py)
# --------------------------------------------------------------------------


def _amount(number: str, unit: str) -> int | None:
    """Seconds from "6" and "hours", or None if either is not one the form
    offers."""
    if unit not in heartbeat.UNITS or not number.strip().isdigit() or len(number.strip()) > 6:
        return None
    return int(number.strip()) * heartbeat.UNITS[unit]


def _in_units(seconds: int) -> tuple[int, str]:
    for unit in ("days", "hours", "minutes"):
        size = heartbeat.UNITS[unit]
        if seconds % size == 0:
            return seconds // size, unit
    return max(1, seconds // 60), "minutes"


def _heartbeat_form(request: Request, config: Config, user: User, target: Target | None, *,
                    error: str | None = None, form: dict | None = None, status_code: int = 200):
    params = dict(target.params or {}) if target else {}
    every_n, every_unit = _in_units(int(heartbeat.period_of(params).total_seconds()))
    grace_n, grace_unit = _in_units(int(heartbeat.grace_of(params).total_seconds()))
    values = form or {"name": target.name if target else "", "every": every_n,
                      "every_unit": every_unit, "grace": grace_n, "grace_unit": grace_unit}
    return templates.TemplateResponse(request, "heartbeat_form.html", {
        "config": config, "user": user,
        "title": "Edit heartbeat" if target else "New heartbeat",
        "target": target, "f": values, "units": list(heartbeat.UNITS), "error": error,
    }, status_code=status_code)


def _check_heartbeat(name: str, every_n: str, every_unit: str, grace_n: str,
                     grace_unit: str) -> tuple[dict | None, str | None]:
    if not name.strip():
        return None, "Give the heartbeat a name: what is the job?"
    period, grace = _amount(every_n, every_unit), _amount(grace_n, grace_unit)
    if period is None or grace is None:
        return None, "How often, and the grace, are each a whole number of minutes, hours or days."
    if not heartbeat.MIN_PERIOD.total_seconds() <= period <= heartbeat.MAX_PERIOD.total_seconds():
        return None, "A heartbeat can be expected every minute at most, and every 31 days at least."
    if not 60 <= grace <= heartbeat.MAX_GRACE.total_seconds():
        return None, "The grace can be from a minute to 7 days."
    return {"period_seconds": period, "grace_seconds": grace}, None


def _ping_page(request: Request, config: Config, user: User, target: Target, token: str):
    """The ping URL, shown this once: only its hash is kept."""
    base = str(request.base_url).rstrip("/")
    url = f"{base}/hb/{token}"
    curl = "curl -fsS -m 10 --retry 3"
    if config.app.tls.mode == "auto" and base.startswith("https://"):
        cert = config.app.tls_dir / tls.CERT_NAME
        if cert.exists():
            # SPARK's own certificate, trusted exactly: -k alone would trust
            # any certificate at all, and --pinnedpubkey is checked even so.
            curl += f' -k --pinnedpubkey "{tls.public_key_pin(cert.read_bytes())}"'
    return templates.TemplateResponse(request, "heartbeat_url.html", {
        "config": config, "user": user, "title": target.name, "target": target,
        "url": url, "curl": curl,
    })


@router.get("/targets/heartbeat/new")
async def new_heartbeat_form(
    request: Request,
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    return _heartbeat_form(request, config, user, None)


@router.post("/targets/heartbeat/new")
async def create_heartbeat(
    request: Request,
    name: str = Form("", max_length=limits.NAME),
    every_n: str = Form("1", alias="every", max_length=limits.SHORT),
    every_unit: str = Form("days", max_length=limits.SHORT),
    grace_n: str = Form("1", alias="grace", max_length=limits.SHORT),
    grace_unit: str = Form("hours", max_length=limits.SHORT),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """A heartbeat is a target that is never probed: judged every minute,
    down at the first miss, up at the first ping."""
    params, error = _check_heartbeat(name, every_n, every_unit, grace_n, grace_unit)
    if error:
        form = {"name": name, "every": every_n, "every_unit": every_unit, "grace": grace_n,
                "grace_unit": grace_unit}
        return _heartbeat_form(request, config, user, None, error=error, form=form,
                               status_code=400)
    token = heartbeat.new_token()
    target = Target(
        name=name.strip(), check_type=CheckType.HEARTBEAT, address="heartbeat", params=params,
        interval_seconds=heartbeat.EVALUATE_SECONDS, failure_threshold=1, recovery_threshold=1,
        heartbeat_hash=heartbeat.token_hash(token), status=HealthStatus.UNKNOWN,
    )
    session.add(target)
    await session.commit()
    events.publish({"kind": "created", "target_id": target.id})
    scheduler_module.schedule_target(target)
    return _ping_page(request, config, user, target, token)


@router.get("/targets/{target_id}/heartbeat")
async def edit_heartbeat_form(
    request: Request,
    target_id: ItemId,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    target = await session.get(Target, target_id)
    if target is None or target.check_type != CheckType.HEARTBEAT:
        return redirect("/targets")
    return _heartbeat_form(request, config, user, target)


@router.post("/targets/{target_id}/heartbeat")
async def update_heartbeat(
    request: Request,
    target_id: ItemId,
    name: str = Form("", max_length=limits.NAME),
    every_n: str = Form("1", alias="every", max_length=limits.SHORT),
    every_unit: str = Form("days", max_length=limits.SHORT),
    grace_n: str = Form("1", alias="grace", max_length=limits.SHORT),
    grace_unit: str = Form("hours", max_length=limits.SHORT),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    target = await session.get(Target, target_id)
    if target is None or target.check_type != CheckType.HEARTBEAT:
        return redirect("/targets")
    params, error = _check_heartbeat(name, every_n, every_unit, grace_n, grace_unit)
    if error:
        form = {"name": name, "every": every_n, "every_unit": every_unit, "grace": grace_n,
                "grace_unit": grace_unit}
        return _heartbeat_form(request, config, user, target, error=error, form=form,
                               status_code=400)
    target.name = name.strip()
    # A failure the job reported stands until it next succeeds.
    target.params = {**dict(target.params or {}), **params}
    await session.commit()
    events.publish({"kind": "updated", "target_id": target.id})
    return redirect("/targets")


@router.post("/targets/{target_id}/heartbeat/url")
async def new_ping_url(
    request: Request,
    target_id: ItemId,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    """A new ping URL. The old one stops working at once."""
    target = await session.get(Target, target_id)
    if target is None or target.check_type != CheckType.HEARTBEAT:
        return redirect("/targets")
    token = heartbeat.new_token()
    target.heartbeat_hash = heartbeat.token_hash(token)
    await session.commit()
    return _ping_page(request, config, user, target, token)


@router.post("/targets/{target_id}/toggle")
async def toggle_target(
    target_id: ItemId,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    target = await session.get(Target, target_id)
    if target is None:
        return redirect("/targets")
    target.enabled = not target.enabled
    if target.enabled:
        target.status = HealthStatus.UNKNOWN
        target.consecutive_failures = 0
        target.consecutive_successes = 0
    else:
        # PAUSED rather than UNKNOWN: "nobody is looking" is a different fact
        # from "nobody has looked yet", and the dashboard counts them apart.
        target.status = HealthStatus.PAUSED
        # An open incident has no knowable end once nobody is checking. Left
        # open it reports an outage that grows for the length of the pause,
        # and resuming sets the status to UNKNOWN so recovery never closes it
        # either -- which is how one target ends up with several open at once.
        await close_open_incidents(session, target.id, resolution=PAUSED_RESOLUTION)
    await session.commit()

    events.publish({"kind": "toggled", "target_id": target.id})
    if target.enabled:
        scheduler_module.schedule_target(target)
        await scheduler_module.run_now(target.id)
    else:
        scheduler_module.unschedule_target(target.id)
    return redirect("/targets")


@router.post("/targets/{target_id}/check")
async def check_target_now(
    target_id: ItemId,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    target = await session.get(Target, target_id)
    if target is None:
        return redirect("/targets")
    # Release this request's transaction before the check runs. Resolving the
    # cookie may have updated the session's last-seen time, which is a write,
    # and SQLite has one writer: the check's own session would otherwise wait
    # on this one until the busy timeout and record "database is locked".
    await session.commit()
    await scheduler_module.run_now(target_id)
    return redirect("/targets")


@router.post("/targets/{target_id}/delete")
async def delete_target(
    target_id: ItemId,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    target = await session.get(Target, target_id)
    if target is not None:
        scheduler_module.unschedule_target(target_id)
        await session.delete(target)
        await session.commit()
        events.publish({"kind": "deleted", "target_id": target_id})
    return redirect("/targets")
