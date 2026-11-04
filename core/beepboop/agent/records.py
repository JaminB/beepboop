"""The agent's database seams: the 90 s window, ``turns`` rows, names for phrases.

* :class:`DbConversation`: ``conversation`` rows (DATA_AND_PRIVACY §6.1), 90 s expiry;
  taint is conversation state (IDENTITY_AND_TRUST §3.2).
* :class:`ConversationTaint`: the registry's ``TaintTracker`` over that row.
* :class:`DbTurnLog`: the ``turns`` row is inserted when the turn starts (``tool_calls``
  rows reference it) and completed at the end with transcript, reply, path, stages,
  tokens and cost; per-profile transcript retention 0 keeps the row without words
  (DATA_AND_PRIVACY §7). "That wasn't me" (VOICE_PIPELINE §8.8) and "forget that"
  (DATA_AND_PRIVACY §9.3) act on the previous row.
* :class:`DbDirectory`: profile names and kinds, cached briefly.
* :func:`explain_turn`: the plain-words "why did you do that" for one turn.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from beepboop.agent.types import (
    CONVERSATION_WINDOW_S,
    ConversationStore,
    PreviousTurn,
    TurnResult,
    TurnStart,
)
from beepboop.db import Database, repo
from beepboop.db.ids import from_ms, now_ms, to_ms
from beepboop.db.repo.conversation import ConversationRow
from beepboop.db.repo.turns import Explanation, TurnRow
from beepboop.policy.types import Principal, ProfileKind

DEFAULT_TRANSCRIPT_DAYS = 30
DEFAULT_GUEST_DAYS = 7


@dataclass
class DbConversation:
    """:class:`~beepboop.agent.types.ConversationStore` over ``repo.conversation``."""

    db: Database
    window_s: int = CONVERSATION_WINDOW_S

    async def load(self, profile_id: str, device: str) -> tuple[list[dict[str, object]], bool]:
        """The live window. Reads with ``fetchall`` so no statement is left open on the
        pooled reader (an unfinished ``fetchone`` cursor pins an old WAL snapshot); an
        expired row is deleted through the writer ("expired rows are deleted by the loop")."""

        def _get(c: sqlite3.Connection) -> ConversationRow | None:
            rows = c.execute(
                "SELECT * FROM conversation WHERE profile_id = ? AND device = ?",
                (profile_id, device),
            ).fetchall()
            return ConversationRow.from_row(rows[0]) if rows else None

        row = await self.db.read(_get)
        if row is None:
            return [], False
        if row.expires_at <= now_ms():
            await self.clear(profile_id, device)
            return [], False
        return row.messages, row.tainted

    async def save(
        self, profile_id: str, device: str, messages: list[dict[str, object]], tainted: bool
    ) -> None:
        row = ConversationRow(
            profile_id, device, messages, tainted, now_ms() + self.window_s * 1000
        )
        await self.db.write(lambda c: repo.conversation.put(c, row))

    async def taint(self, profile_id: str, device: str) -> None:
        def _taint(c: sqlite3.Connection) -> None:
            existing = repo.conversation.get(c, profile_id, device, now_ms())
            if existing is None:
                repo.conversation.put(
                    c,
                    ConversationRow(profile_id, device, [], True, now_ms() + self.window_s * 1000),
                )
            else:
                repo.conversation.set_tainted(c, profile_id, device)

        await self.db.write(_taint)

    async def clear(self, profile_id: str, device: str) -> None:
        await self.db.write(lambda c: repo.conversation.delete(c, profile_id, device))


@dataclass
class ConversationTaint:
    """``TaintTracker`` for the registry: an untrusted result taints the window row."""

    conversation: ConversationStore

    async def taint(self, principal: Principal) -> None:
        if principal.profile_id == "guest":  # no window beyond the turn (DATA_AND_PRIVACY §8)
            return
        await self.conversation.taint(principal.profile_id, principal.device)


def _retention_days(c: sqlite3.Connection, profile_id: str) -> int:
    if profile_id == "guest":
        value = repo.household.get(c, "guest_turn_retention_days")
        return int(value) if isinstance(value, int | float) else DEFAULT_GUEST_DAYS
    prof = repo.profiles.get(c, profile_id)
    if prof is not None and prof.transcript_retention_days is not None:
        return prof.transcript_retention_days
    value = repo.household.get(c, "transcript_retention_days")
    return int(value) if isinstance(value, int | float) else DEFAULT_TRANSCRIPT_DAYS


@dataclass
class DbTurnLog:
    """:class:`~beepboop.agent.types.TurnLog` over ``repo.turns`` (trace writes)."""

    db: Database

    async def start(self, turn: TurnStart) -> None:
        p = turn.principal
        via = p.via
        row = TurnRow(
            id=turn.turn_id,
            profile_id=p.profile_id,
            via=via,
            locale=turn.locale,
            path="llm",
            stt_provider=turn.stt_provider,
            tts_provider=turn.tts_provider,
            device=turn.device,
            actor_profile_id=p.actor_profile_id,
            confidence=p.confidence,
            margin=p.margin,
            id_stage=turn.id_stage,
            created_at=to_ms(turn.created_at),
        )
        await self.db.write(lambda c: repo.turns.insert_turn(c, row))

    async def finish(self, turn: TurnStart, transcript: str, result: TurnResult) -> None:
        p = turn.principal

        def _finish(c: sqlite3.Connection) -> None:
            keep = _retention_days(c, p.profile_id) > 0
            row = TurnRow(
                id=turn.turn_id,
                profile_id=p.profile_id,
                via=p.via,
                locale=turn.locale,
                path=result.path,
                stt_provider=turn.stt_provider,
                tts_provider=turn.tts_provider,
                transcript=transcript if keep else None,
                reply=(result.reply_text or None) if keep else None,
                stages_ms=dict(result.stages_ms),
                tools_offered=result.tools_offered,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                tokens_cached=result.tokens_cached,
                cost_micro_usd=result.cost_micro_usd,
                llm_provider=result.llm_provider,
                tainted=result.tainted,
                error=result.error,
            )
            repo.turns.finish_turn(c, row)

        await self.db.write(_finish)

    async def previous(self, device: str, before_turn_id: str) -> PreviousTurn | None:
        row = await self.db.read(lambda c: repo.turns.previous_on_device(c, device, before_turn_id))
        if row is None:
            return None
        return PreviousTurn(row.id, row.profile_id, from_ms(row.created_at))

    async def not_me(self, turn_id: str, *, corrector: Principal, disputed: bool) -> None:
        """§8.8 steps 1, 2, 4 and 5. Step 3 (negative embedding sample) needs the turn's
        embedding, which core does not keep; it is the voiceprint builder's (pending)."""

        def _flag(c: sqlite3.Connection) -> None:
            if disputed:
                c.execute("UPDATE turns SET flagged = 1 WHERE id = ?", (turn_id,))
            else:
                repo.turns.set_corrected(c, turn_id, "guest")
            repo.audit.record(
                c,
                actor_id=None if corrector.profile_id == "guest" else corrector.profile_id,
                actor_name=corrector.profile_id,
                via="voice",
                action="turn.not_me",
                target=turn_id,
                details={"disputed": disputed},
            )

        await self.db.write(_flag, durable=True)

    async def forget(self, turn_id: str) -> bool:
        return await self.db.write(lambda c: repo.turns.delete_turn(c, turn_id), durable=True)

    async def flag(self, turn_id: str) -> None:
        await self.db.write(lambda c: repo.turns.set_flagged(c, turn_id))


@dataclass
class DbDirectory:
    """:class:`~beepboop.agent.types.ProfileDirectory` from ``profiles``, refreshed at most
    every ``ttl_s`` seconds (called by the loop at the start of each turn)."""

    db: Database
    ttl_s: float = 30.0
    _names: dict[str, str] = field(default_factory=dict[str, str])
    _kinds: dict[str, ProfileKind] = field(default_factory=dict[str, ProfileKind])
    _admin: str = "an admin"
    _loaded_at: float | None = None

    async def refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._loaded_at is not None and now - self._loaded_at < self.ttl_s:
            return
        rows = await self.db.read(repo.profiles.list_)
        self._names = {r.id: r.name for r in rows}
        self._kinds = {r.id: r.kind for r in rows}
        admins = sorted(r.name for r in rows if r.kind == "admin")
        self._admin = admins[0] if admins else "an admin"
        self._loaded_at = now

    def kind(self, profile_id: str) -> ProfileKind:
        if profile_id == "guest":
            return "guest"
        return self._kinds.get(profile_id, "member")

    def name(self, profile_id: str) -> str:
        if profile_id == "guest":
            return "Guest"
        return self._names.get(profile_id, "there")

    def admin_name(self) -> str:
        return self._admin


def explain_turn(ex: Explanation, *, names: dict[str, str] | None = None) -> str:
    """The Activity page's "why did you do that" (IDENTITY_AND_TRUST §11) in plain words."""
    n = names or {}
    t = ex.turn
    who = n.get(t.profile_id, "a guest" if t.profile_id == "guest" else t.profile_id)
    how = (
        f"recognised by voice ({t.confidence:.2f} confidence, {t.margin or 0:.2f} margin)"
        if t.via == "voice" and t.confidence is not None
        else f"signed in on the {t.via}"
    )
    lines = [f"Heard from {who}, {how}."]
    if t.transcript:
        lines.append(f'They said: "{t.transcript}".')
    planner = {
        "fast": "The fast path handled it without the language model.",
        "llm": f"The language model ({t.llm_provider or 'unknown'}) planned it.",
        "offline": "The language model was not reachable.",
        "error": "Something went wrong.",
    }[t.path]
    lines.append(planner)
    for call in ex.tool_calls:
        if call.decision == "allow":
            why = f"allowed by grant {call.grant_id}" if call.grant_id else "allowed"
        elif call.decision == "confirm":
            why = "held for approval on the phone"
        else:
            why = f"refused ({call.deny_reason})"
        lines.append(f"{call.tool}: {why}, result {call.result_status}.")
    if t.tainted:
        lines.append("Text from a tool result was in the conversation, so changes needed approval.")
    if t.flagged:
        lines.append("Someone said this wasn't them.")
    return " ".join(lines)


__all__ = [
    "ConversationTaint",
    "DbConversation",
    "DbDirectory",
    "DbTurnLog",
    "explain_turn",
]
