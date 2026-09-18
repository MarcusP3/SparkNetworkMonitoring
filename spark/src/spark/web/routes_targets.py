"""Target management: list, create, edit, delete, and check-on-demand.

There is no discovery yet, so this is the only way targets get into the
database. That makes it load-bearing rather than a convenience: without it the
check engine has nothing to run, and "edit the database by hand over SSH" is
exactly the experience the Setting model was written to avoid.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import events
from .. import scheduler as scheduler_module
from ..config import Config
from ..models import (
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_RECOVERY_THRESHOLD,
    DEFAULT_TIMEOUT_SECONDS,
    CheckType,
    HealthStatus,
    Incident,
    Target,
    User,
)
from .deps import get_config, get_session, redirect, require_user, templates

router = APIRouter()

# Sensible starting points per check type, shown as the form's placeholder so
# the params box is documentation rather than a mystery.
PARAM_HINTS: dict[str, str] = {
    "ping": '{"count": 3, "loss_warn_percent": 1}',
    "tcp": '{"port": 443}',
    "http": '{"expect_status": 200, "expect_body": "", "cert_warn_days": 14}',
    "dns": '{"rdtype": "A", "server": "", "expect": ""}',
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
        target.interval_seconds != DEFAULT_INTERVAL_SECONDS
        or float(target.timeout_seconds) != DEFAULT_TIMEOUT_SECONDS
        or target.failure_threshold != DEFAULT_FAILURE_THRESHOLD
        or target.recovery_threshold != DEFAULT_RECOVERY_THRESHOLD
    )


async def _form_context(
    session: AsyncSession, config: Config, user: User, target: Target | None, **extra
) -> dict:
    others = list(
        (
            await session.execute(
                select(Target).where(Target.id != (target.id if target else -1)).order_by(Target.name)
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
        "check_types": [t.value for t in CheckType if t is not CheckType.DOCKER],
        "param_hints": PARAM_HINTS,
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
    return templates.TemplateResponse(
        request,
        "targets.html",
        {
            "config": config,
            "user": user,
            "title": "Targets",
            "targets": targets,
            "open_counts": open_counts,
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
    name: str = Form(...),
    check_type: str = Form(...),
    address: str = Form(...),
    interval_seconds: int = Form(DEFAULT_INTERVAL_SECONDS),
    timeout_seconds: float = Form(DEFAULT_TIMEOUT_SECONDS),
    failure_threshold: int = Form(DEFAULT_FAILURE_THRESHOLD),
    recovery_threshold: int = Form(DEFAULT_RECOVERY_THRESHOLD),
    depends_on_target_id: str = Form(""),
    params: str = Form(""),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    parsed, error = _parse_params(params)
    if error:
        context = await _form_context(
            session, config, user, None, error=error, submitted=await request.form()
        )
        return templates.TemplateResponse(request, "target_form.html", context, status_code=400)

    target = Target(
        name=name.strip(),
        check_type=CheckType(check_type),
        address=address.strip(),
        params=parsed,
        interval_seconds=max(5, int(interval_seconds)),
        timeout_seconds=float(timeout_seconds),
        failure_threshold=max(1, int(failure_threshold)),
        recovery_threshold=max(1, int(recovery_threshold)),
        depends_on_target_id=int(depends_on_target_id) if depends_on_target_id else None,
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
    target_id: int,
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    target = await session.get(Target, target_id)
    if target is None:
        return redirect("/targets")
    context = await _form_context(session, config, user, target)
    return templates.TemplateResponse(request, "target_form.html", context)


@router.post("/targets/{target_id}/edit")
async def update_target(
    request: Request,
    target_id: int,
    name: str = Form(...),
    check_type: str = Form(...),
    address: str = Form(...),
    interval_seconds: int = Form(DEFAULT_INTERVAL_SECONDS),
    timeout_seconds: float = Form(DEFAULT_TIMEOUT_SECONDS),
    failure_threshold: int = Form(DEFAULT_FAILURE_THRESHOLD),
    recovery_threshold: int = Form(DEFAULT_RECOVERY_THRESHOLD),
    depends_on_target_id: str = Form(""),
    params: str = Form(""),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    target = await session.get(Target, target_id)
    if target is None:
        return redirect("/targets")

    parsed, error = _parse_params(params)
    if error:
        context = await _form_context(session, config, user, target, error=error)
        return templates.TemplateResponse(request, "target_form.html", context, status_code=400)

    target.name = name.strip()
    target.check_type = CheckType(check_type)
    target.address = address.strip()
    target.params = parsed
    target.interval_seconds = max(5, int(interval_seconds))
    target.timeout_seconds = float(timeout_seconds)
    target.failure_threshold = max(1, int(failure_threshold))
    target.recovery_threshold = max(1, int(recovery_threshold))
    target.depends_on_target_id = int(depends_on_target_id) if depends_on_target_id else None
    await session.commit()

    events.publish({"kind": "updated", "target_id": target.id})
    if target.enabled:
        scheduler_module.schedule_target(target)
    return redirect("/targets")


@router.post("/targets/{target_id}/toggle")
async def toggle_target(
    target_id: int,
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
    target_id: int,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    target = await session.get(Target, target_id)
    if target is None:
        return redirect("/targets")
    await scheduler_module.run_now(target_id)
    return redirect("/targets")


@router.post("/targets/{target_id}/delete")
async def delete_target(
    target_id: int,
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
