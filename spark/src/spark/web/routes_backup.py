"""Settings -> Backup: download a backup, encrypted with a passphrase (backup.py).

Every download is encrypted: the file holds secret.key, so it opens every
stored credential. The passphrase is typed twice, used once, and never
stored. A download is a security event (Discord, if on), like a password
change: if it was not you, you want to know.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from .. import alerts, backup, limits
from ..auth import client_ip
from ..config import Config
from ..models import User
from .deps import get_config, get_session, require_user
from .routes_settings import _render

router = APIRouter()


@router.post("/settings/backup/download")
async def download_backup(
    request: Request,
    which: str = Form("now", max_length=limits.NAME),
    passphrase: str = Form("", max_length=limits.PASSWORD),
    passphrase_confirm: str = Form("", max_length=limits.PASSWORD),
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    data_dir = config.app.data_dir
    try:
        backup.check_passphrase(passphrase, passphrase_confirm)
        if which == "now":
            path = await asyncio.to_thread(backup.fresh_encrypted, data_dir, passphrase,
                                           instance=config.app.instance_name)
            made = None
        else:
            source = backup.stored_path(data_dir, which)
            made = next((s.made for s in backup.listing(data_dir) if s.name == which), None)
            path = await asyncio.to_thread(backup.encrypted_copy, source, data_dir, passphrase)
    except backup.BackupError as exc:
        return await _render(request, session, config, user, section="backup",
                             backup_error=str(exc), status_code=400)

    ip = client_ip(request, config.auth)
    await alerts.on_security_event(
        session, kind="security_backup", tone="info",
        subject=f"A backup was downloaded{f' from {ip}' if ip else ''}",
        body="It holds every stored credential, encrypted with the passphrase typed at "
             "download. If this was not you, change the password and every credential "
             "SPARK stores (SNMP, TrueNAS, Proxmox, the Discord webhook).")
    await session.commit()
    return FileResponse(
        path, media_type="application/octet-stream",
        filename=backup.download_name(config.app.instance_name, made),
        background=BackgroundTask(Path(path).unlink, missing_ok=True),
    )
