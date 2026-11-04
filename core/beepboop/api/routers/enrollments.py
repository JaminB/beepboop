"""``/api/enrollments``: People > <name> > Voice (WEB_UI §5.3, VOICE_PIPELINE §9).

Consent first (DATA_AND_PRIVACY §9.1; for a child an admin consents and is recorded),
then the audio service records on the device's own microphone. The browser never sends
audio; progress arrives as SSE ``enroll`` events.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from beepboop.api.deps import ApiDeps, Deps, Session, WebSession, can_manage_profile, record_audit
from beepboop.api.enroll import CONSENT_VERSION, EnrollmentError, EnrollmentHub, EnrollState
from beepboop.api.problem import ApiError
from beepboop.api.routers.profiles import load_visible

router = APIRouter(prefix="/api/enrollments", tags=["enrollments"])


class EnrollStart(BaseModel):
    profile_id: str
    consent: Literal[True]
    consent_version: str = CONSENT_VERSION


class EnrollView(BaseModel):
    session_id: str
    profile_id: str
    step: str
    prompt: str
    progress: int
    total: int
    done: bool
    next_text: str | None


class ConsentText(BaseModel):
    version: str
    paragraphs: list[str]


CONSENT_PARAGRAPHS = [
    "A voiceprint is a set of numbers computed from how you say a few phrases. It lets me "
    "guess who is talking so I can use your timers, lists and calendar.",
    "It is stored encrypted on this device only and is never sent anywhere.",
    "The recordings it is made from are discarded as soon as each clip is measured. "
    "I never keep recordings.",
    "Voice is a guess, not a password: anything sensitive still needs your phone.",
    "You can delete your voiceprint at any time with one tap.",
]


def _hub(deps: ApiDeps) -> EnrollmentHub:
    assert deps.enroll_hub is not None  # noqa: S101 - set in ApiDeps.__post_init__
    return deps.enroll_hub


def _view(state: EnrollState) -> EnrollView:
    return EnrollView.model_validate(state.view())


@router.get("/consent", operation_id="enrollments_consent")
async def enrollments_consent() -> ConsentText:
    return ConsentText(version=CONSENT_VERSION, paragraphs=CONSENT_PARAGRAPHS)


@router.post("", operation_id="enrollments_start", status_code=201)
async def enrollments_start(body: EnrollStart, session: Session, deps: Deps) -> EnrollView:
    target = await load_visible(deps, session, body.profile_id)
    if not can_manage_profile(session.profile, target):
        raise ApiError(403, "forbidden")
    if target.kind == "child" and not session.is_admin:
        raise ApiError(403, "child_needs_admin")
    if target.kind == "guest":
        raise ApiError(409, "guest_not_enrollable")
    if body.consent_version != CONSENT_VERSION:
        raise ApiError(409, "consent_outdated")
    if not deps.enrollment.available:
        raise ApiError(503, "audio_offline")
    consented_by = session.profile.id if target.kind == "child" else None
    try:
        state = _hub(deps).start(
            target.id, target.kind, consent_version=body.consent_version, consented_by=consented_by
        )
    except EnrollmentError as exc:
        raise ApiError(409, exc.code) from exc
    try:
        await deps.enrollment.start(state.session_id, target.id, state.prompts())
    except Exception as exc:
        _hub(deps).cancel(state.session_id)
        raise ApiError(503, "audio_offline") from exc
    await record_audit(deps, session.profile, "enroll.started", target=target.id)
    return _view(state)


async def _own_session(deps: ApiDeps, session: WebSession, session_id: str) -> EnrollState:
    state = _hub(deps).get(session_id)
    if state is None:
        raise ApiError(404, "not_found")
    target = await load_visible(deps, session, state.profile_id)
    if not can_manage_profile(session.profile, target):
        raise ApiError(404, "not_found")
    return state


@router.get("/{session_id}", operation_id="enrollments_read")
async def enrollments_read(session_id: str, session: Session, deps: Deps) -> EnrollView:
    return _view(await _own_session(deps, session, session_id))


@router.post("/{session_id}/redo", operation_id="enrollments_redo")
async def enrollments_redo(session_id: str, session: Session, deps: Deps) -> EnrollView:
    await _own_session(deps, session, session_id)
    state = _hub(deps).redo_last(session_id)
    assert state is not None  # noqa: S101 - checked above
    return _view(state)


@router.post("/{session_id}/finish", operation_id="enrollments_finish")
async def enrollments_finish(session_id: str, session: Session, deps: Deps) -> EnrollView:
    state = await _own_session(deps, session, session_id)
    try:
        await _hub(deps).finish(session_id)
    except EnrollmentError as exc:
        raise ApiError(409 if exc.code == "not_done" else 503, exc.code) from exc
    await deps.enrollment.stop(session_id)
    await deps.refresh_policy()
    await record_audit(deps, session.profile, "enroll.finished", target=state.profile_id)
    return _view(state)


@router.delete("/{session_id}", operation_id="enrollments_cancel", status_code=204)
async def enrollments_cancel(session_id: str, session: Session, deps: Deps) -> None:
    await _own_session(deps, session, session_id)
    _hub(deps).cancel(session_id)
    await deps.enrollment.stop(session_id)


__all__ = ["router"]
