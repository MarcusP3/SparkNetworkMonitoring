"""Alerts -> Suppressions: one alert rule, for one device, off or with its
own line (suppressions.py)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits, suppressions
from ..config import Config
from ..models import AlertSuppression, User
from .deps import ItemId, get_config, get_session, redirect, require_user
from .routes_settings import _render

router = APIRouter()

PAGE = "/alerts/suppressions"


@router.post("/settings/suppressions")
async def save_suppression(
    request: Request,
    device_id: str = Form("", max_length=limits.SHORT),
    rule: str = Form("", max_length=limits.SHORT),
    mode: str = Form("off", max_length=limits.SHORT),
    threshold: str = Form("", max_length=limits.SHORT),
    detail: str = Form("", max_length=limits.NAME),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    try:
        row = await suppressions.save(
            session, device_id=limits.as_id(device_id) if device_id.strip() else None,
            rule=rule, mode=mode, threshold=threshold, detail=detail)
    except suppressions.SuppressionError as exc:
        # Refused before anything was changed: nothing to roll back.
        return await _render(request, session, config, user, section="suppressions",
                             supp_error=str(exc),
                             supp_form={"device": device_id, "rule": rule, "mode": mode,
                                        "threshold": threshold, "detail": detail},
                             status_code=400)
    await session.commit()
    return redirect(f"{PAGE}?saved=1#supp-{row.id}")


@router.post("/settings/suppressions/{supp_id}/delete")
async def remove_suppression(
    supp_id: ItemId,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    row = await session.get(AlertSuppression, supp_id)
    if row is not None:
        await suppressions.remove(session, row)
        await session.commit()
    return redirect(PAGE)
