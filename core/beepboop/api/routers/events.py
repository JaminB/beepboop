"""``GET /api/events``: the per-session Server-Sent Events stream (WEB_UI §8).

On connect the stream first replays this profile's live approvals (with their nonces,
which only ever travel here) and extends each once to 120 s (IDENTITY_AND_TRUST §8.2:
"the approver's session opens the approvals stream in time"). Then it relays bus events
filtered by §6.4, resumes from ``Last-Event-ID`` and pings every 15 s.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from beepboop.api.deps import Deps, Session
from beepboop.api.events import Viewer, approval_payload, sse_stream
from beepboop.api.routers.approvals import extend_pending_for

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get(
    "",
    operation_id="events_stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def events_stream(
    session: Session,
    deps: Deps,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    await extend_pending_for(deps, session.profile.id)
    initial = [
        ("approval", approval_payload(record, nonce))
        for record, nonce in deps.events.pending_for(session.profile.id, deps.clock())
    ]
    last = int(last_event_id) if last_event_id and last_event_id.isdigit() else None
    stream = sse_stream(
        deps.events,
        Viewer(session.profile.id, session.profile.kind),
        last_event_id=last,
        initial=initial,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
