"""``GET /api/me``: the session's profile and web scope (docs/WEB_UI.md §6)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from beepboop.api.deps import Session, web_scope

router = APIRouter(prefix="/api/me", tags=["me"])


class Me(BaseModel):
    profile_id: str
    name: str
    kind: str
    locale: str
    scopes: list[str]


@router.get("", operation_id="me_read")
async def me_read(session: Session) -> Me:
    p = session.profile
    return Me(
        profile_id=p.id,
        name=p.name,
        kind=p.kind,
        locale=p.locale,
        scopes=sorted(web_scope(p)),
    )
