"""Voice enrollment sessions behind People > <name> > Voice (WEB_UI §5.3, VOICE_PIPELINE §9).

The browser is the teleprompter; the device's array records. The API starts a session
(after consent), the :class:`~beepboop.api.ports.EnrollmentChannel` sends
``enroll.start`` to the audio service, and the audio bridge hands each ``enroll.clip``
result to :meth:`EnrollmentHub.clip`. The hub tracks progress through the §9.2 steps and
publishes an SSE ``enroll`` event per clip. Accepted embeddings are held in memory for the
session only and passed to the :class:`VoiceprintSink` at the end (centroids, similarity
and calibration are §9.3, owned by the voice builder). No audio exists here (ADR 0018).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from beepboop.api.events import EventBus
from beepboop.policy.types import ProfileKind
from beepboop_protocol import EnrollClipData, new_ulid

CONSENT_VERSION = "2026-11-01"


@dataclass(frozen=True, slots=True)
class Step:
    key: str
    prompt: str
    clips: int
    mode: str  # "wake" | "free"


#: VOICE_PIPELINE §9.2 (adult column; children get the second plan).
ADULT_PLAN: tuple[Step, ...] = (
    Step(
        "near",
        "Stand where you usually talk to me. Say “Hey beepboop” when the ring turns blue.",
        5,
        "wake",
    ),
    Step("far", "Now from across the room.", 3, "wake"),
    Step("read", "Read the sentence on screen.", 5, "free"),
    Step("doorway", "From the kitchen or doorway, read two more.", 2, "free"),
)
CHILD_PLAN: tuple[Step, ...] = (
    Step("near", "Say “Hey beepboop” when the ring turns blue.", 8, "wake"),
    Step("far", "Now from across the room.", 5, "wake"),
    Step("read", "Read the sentence on screen.", 8, "free"),
    Step("doorway", "From the doorway, read three more.", 3, "free"),
)
SENTENCES: tuple[str, ...] = (
    "The quick brown fox jumps over the lazy dog.",
    "Please turn on the lights in the living room.",
    "What will the weather be like tomorrow morning?",
    "Add oat milk and fresh bread to the shopping list.",
    "Set a timer for twelve minutes for the pasta.",
    "Thick fog rolled over the harbour at dawn.",
    "Remind me to call the vet on Thursday.",
    "Jazz music plays softly in the kitchen.",
)


def plan_for(kind: ProfileKind) -> tuple[Step, ...]:
    return CHILD_PLAN if kind == "child" else ADULT_PLAN


class VoiceprintSink(Protocol):
    """Builds and stores the voiceprint from accepted embeddings (VOICE_PIPELINE §9.3)."""

    async def save(
        self,
        profile_id: str,
        embeddings: Sequence[Sequence[float]],
        *,
        consent_version: str,
        consented_by: str | None,
    ) -> None: ...


@dataclass
class EnrollState:
    session_id: str
    profile_id: str
    kind: ProfileKind
    consent_version: str
    consented_by: str | None
    step_index: int = 0
    accepted: list[int] = field(default_factory=list[int])
    embeddings: list[list[float]] = field(default_factory=list[list[float]])
    last_clip_step: int | None = None
    done: bool = False

    @property
    def plan(self) -> tuple[Step, ...]:
        return plan_for(self.kind)

    @property
    def total(self) -> int:
        return sum(s.clips for s in self.plan)

    @property
    def progress(self) -> int:
        return sum(self.accepted)

    def prompts(self) -> list[str]:
        out: list[str] = []
        sentence = 0
        for s in self.plan:
            for _ in range(s.clips):
                if s.mode == "free":
                    out.append(SENTENCES[sentence % len(SENTENCES)])
                    sentence += 1
                else:
                    out.append("Hey beepboop")
        return out

    def view(self) -> dict[str, object]:
        step = self.plan[min(self.step_index, len(self.plan) - 1)]
        return {
            "session_id": self.session_id,
            "profile_id": self.profile_id,
            "step": step.key,
            "prompt": step.prompt,
            "progress": self.progress,
            "total": self.total,
            "done": self.done,
            "next_text": None if self.done else self.prompts()[min(self.progress, self.total - 1)],
        }


class EnrollmentError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class EnrollmentHub:
    bus: EventBus
    sink: VoiceprintSink | None = None
    _sessions: dict[str, EnrollState] = field(default_factory=dict[str, EnrollState])

    def start(
        self,
        profile_id: str,
        kind: ProfileKind,
        *,
        consent_version: str,
        consented_by: str | None,
    ) -> EnrollState:
        if kind == "guest":
            raise EnrollmentError("guest_not_enrollable")
        for sid, s in list(self._sessions.items()):
            if s.profile_id == profile_id:
                del self._sessions[sid]
        state = EnrollState(
            new_ulid(),
            profile_id,
            kind,
            consent_version,
            consented_by,
            accepted=[0] * len(plan_for(kind)),
        )
        self._sessions[state.session_id] = state
        return state

    def get(self, session_id: str) -> EnrollState | None:
        return self._sessions.get(session_id)

    def clip(self, data: EnrollClipData) -> EnrollState | None:
        """One ``enroll.clip`` from the audio service (called by the audio bridge)."""
        state = self._sessions.get(data.session_id)
        if state is None or state.done:
            return None
        if data.accepted and data.embedding is not None:
            state.accepted[state.step_index] += 1
            state.embeddings.append(list(data.embedding))
            state.last_clip_step = state.step_index
            if state.accepted[state.step_index] >= state.plan[state.step_index].clips:
                state.step_index += 1
            if state.step_index >= len(state.plan):
                state.done = True
        q = data.quality
        self.bus.publish(
            "enroll",
            {
                "session_id": state.session_id,
                "step": state.plan[min(state.step_index, len(state.plan) - 1)].key,
                "clip_quality": {
                    "snr": q.snr_db,
                    "clipping": q.clipping,
                    "speech_ratio": q.speech_ratio,
                    "duration_ms": q.duration_ms,
                },
                "accepted": data.accepted,
                "progress": state.progress,
                "total": state.total,
                "done": state.done,
            },
            audience="subject_and_admins",
            subject_profile_id=state.profile_id,
        )
        return state

    def redo_last(self, session_id: str) -> EnrollState | None:
        state = self._sessions.get(session_id)
        if state is None or state.last_clip_step is None or not state.embeddings:
            return state
        step = state.last_clip_step
        state.embeddings.pop()
        state.accepted[step] = max(0, state.accepted[step] - 1)
        state.step_index = step
        state.done = False
        state.last_clip_step = None
        return state

    def cancel(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def finish(self, session_id: str) -> EnrollState:
        state = self._sessions.get(session_id)
        if state is None:
            raise EnrollmentError("no_session")
        if not state.done:
            raise EnrollmentError("not_done")
        if self.sink is None:
            raise EnrollmentError("voiceprint_builder_unavailable")
        await self.sink.save(
            state.profile_id,
            state.embeddings,
            consent_version=state.consent_version,
            consented_by=state.consented_by,
        )
        del self._sessions[session_id]
        return state


__all__ = [
    "ADULT_PLAN",
    "CHILD_PLAN",
    "CONSENT_VERSION",
    "EnrollState",
    "EnrollmentError",
    "EnrollmentHub",
    "VoiceprintSink",
    "plan_for",
]
