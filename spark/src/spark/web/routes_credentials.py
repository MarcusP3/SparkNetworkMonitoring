"""Settings -> Credentials: API keys for devices' own APIs (credentials.py)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import credentials, limits
from ..config import Config
from ..models import ApiCredential, User
from ..vault import vault_for
from .deps import ItemId, get_config, get_session, redirect, require_user
from .routes_settings import _render

router = APIRouter()

PAGE = "/settings/credentials"


def _form(kind: str, name: str, device_id: str, host: str) -> dict:
    """What was typed, to put back on the page after an error. Never the key."""
    return {"kind": kind, "name": name, "device_id": device_id, "host": host}


@router.post("/settings/credentials")
async def add_credential(
    request: Request,
    kind: str = Form("truenas", max_length=limits.SHORT),
    name: str = Form("", max_length=limits.NAME),
    device_id: str = Form("", max_length=limits.SHORT),
    host: str = Form("", max_length=limits.ADDRESS),
    api_key: str = Form("", max_length=limits.SECRET),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    try:
        row = await credentials.add(session, vault_for(config), kind=kind, name=name,
                                    device_id=limits.as_id(device_id) if device_id.strip() else None,
                                    host=host, api_key=api_key)
    except credentials.CredentialError as exc:
        return await _render(request, session, config, user, section="credentials",
                             cred_error=str(exc), cred_form=_form(kind, name, device_id, host),
                             status_code=400)
    # Straight to a Test: it fetches the certificate (sending nothing) so
    # the page can ask for it to be trusted.
    await credentials.test(session, vault_for(config), row)
    await session.commit()
    return redirect(f"{PAGE}#cred-{row.id}")


@router.post("/settings/credentials/{cred_id}")
async def edit_credential(
    request: Request,
    cred_id: ItemId,
    name: str = Form("", max_length=limits.NAME),
    device_id: str = Form("", max_length=limits.SHORT),
    host: str = Form("", max_length=limits.ADDRESS),
    api_key: str = Form("", max_length=limits.SECRET),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    row = await session.get(ApiCredential, cred_id)
    if row is None:
        return redirect(PAGE)
    try:
        await credentials.update(session, vault_for(config), row, name=name,
                                 device_id=limits.as_id(device_id) if device_id.strip() else None,
                                 host=host, api_key=api_key)
    except credentials.CredentialError as exc:
        await session.rollback()
        return await _render(request, session, config, user, section="credentials",
                             cred_error=str(exc), status_code=400)
    await session.commit()
    return redirect(f"{PAGE}#cred-{cred_id}")


def _back(back: str, cred_id: int) -> str:
    """Back to the device page Test was pressed on, or to this list."""
    from .routes_auth import _safe_next

    target = _safe_next(back) if back else ""
    if target.startswith("/devices/"):
        return f"{target}#api-{cred_id}"
    return f"{PAGE}#cred-{cred_id}"


@router.post("/settings/credentials/{cred_id}/test")
async def test_credential(
    cred_id: ItemId,
    back: str = Form("", max_length=limits.URL),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    _user: User = Depends(require_user),
):
    row = await session.get(ApiCredential, cred_id)
    if row is not None:
        await credentials.test(session, vault_for(config), row)
        await session.commit()
    return redirect(_back(back, cred_id))


@router.post("/settings/credentials/{cred_id}/trust")
async def trust_certificate(
    request: Request,
    cred_id: ItemId,
    fingerprint: str = Form("", max_length=limits.NAME * 2),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    row = await session.get(ApiCredential, cred_id)
    if row is None:
        return redirect(PAGE)
    try:
        await credentials.trust(session, vault_for(config), row, fingerprint)
    except credentials.CredentialError as exc:
        return await _render(request, session, config, user, section="credentials",
                             cred_error=str(exc), status_code=400)
    await session.commit()
    return redirect(f"{PAGE}#cred-{cred_id}")


@router.post("/settings/credentials/{cred_id}/delete")
async def delete_credential(
    cred_id: ItemId,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_user),
):
    row = await session.get(ApiCredential, cred_id)
    if row is not None:
        await credentials.forget_alert(session, row.id)
        await session.delete(row)
        await session.commit()
    return redirect(PAGE)
