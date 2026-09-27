"""The service map page: /map."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import servicemap
from ..config import Config
from ..models import User
from .deps import get_config, get_session, require_user, templates

router = APIRouter()


@router.get("/map")
async def service_map(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
    config: Config = Depends(get_config),
    user: User = Depends(require_user),
):
    # A search longer than anything it could match is not a search.
    query = q.strip()[:100]
    result = await servicemap.build(session, query=query)
    return templates.TemplateResponse(
        request,
        "map.html",
        {"config": config, "user": user, "title": "Service map", "map": result, "q": query},
    )
