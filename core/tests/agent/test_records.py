"""The agent's database seams on a real migrated SQLite file (DATA_AND_PRIVACY §6.1, §7, §9.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from beepboop.agent import (
    FastPath,
    ListSpeaker,
    NoLLM,
    PromptInputs,
    TurnLoop,
    TurnResult,
    TurnStart,
    Utterance,
)
from beepboop.agent.records import (
    ConversationTaint,
    DbConversation,
    DbDirectory,
    DbTurnLog,
    explain_turn,
)
from beepboop.agent.wiring import build_turn_loop, load_provider
from beepboop.db import Database, repo
from beepboop.policy import ALL_SCOPES, ApprovalService, MemoryApprovalStore, Principal
from beepboop.providers.llm.fallback import Ladder
from beepboop.registry import Registry
from beepboop.registry.registry import ToolCallAudit
from beepboop.service import DbAudit


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "beepboop.db", batch_window_s=0.0)
    yield database
    await database.close()


async def _profile(db: Database, name: str, kind: str = "member") -> str:
    row = await db.write(lambda c: repo.profiles.insert(c, name, kind, "critical"))  # type: ignore[arg-type]
    return row.id


def _p(profile_id: str, turn_id: str, device: str = "kitchen") -> Principal:
    return Principal(profile_id, "voice", 0.9, 0.3, device, ALL_SCOPES, False, turn_id)


async def test_conversation_window_and_taint(db: Database) -> None:
    alex = await _profile(db, "Alex")
    conv = DbConversation(db)
    assert await conv.load(alex, "kitchen") == ([], False)
    await conv.save(alex, "kitchen", [{"role": "user", "content": []}], False)
    msgs, tainted = await conv.load(alex, "kitchen")
    assert len(msgs) == 1
    assert not tainted
    await ConversationTaint(conv).taint(_p(alex, "T1"))
    assert (await conv.load(alex, "kitchen"))[1] is True
    await conv.clear(alex, "kitchen")
    assert await conv.load(alex, "kitchen") == ([], False)
    await ConversationTaint(conv).taint(_p(alex, "T1"))  # no row yet: taint creates one
    assert (await conv.load(alex, "kitchen"))[1] is True
    expired = DbConversation(db, window_s=-1)
    await expired.save(alex, "kitchen", [], False)
    assert await expired.load(alex, "kitchen") == ([], False)


async def test_turn_rows_written_before_tool_calls(db: Database) -> None:
    alex = await _profile(db, "Alex")
    log = DbTurnLog(db)
    start = TurnStart(
        "01JBX6Q8Y4H3K9ZR2M7V5T8WDA",
        _p(alex, "01JBX6Q8Y4H3K9ZR2M7V5T8WDA"),
        "en-US",
        "kitchen",
        "sherpa-zipformer",
        "sherpa-piper",
        "final",
        datetime(2026, 11, 3, tzinfo=UTC),
    )
    await log.start(start)
    from beepboop.policy.types import Decision

    await DbAudit(db).record(
        ToolCallAudit(
            start.turn_id,
            start.principal,
            "timer.set",
            "timers",
            {"kind": "timer"},
            [],
            Decision("allow", "act", grant_id="g1"),
            "ok",
            False,
            12,
        )
    )
    result = TurnResult(start.turn_id, "fast", tokens_in=5, cost_micro_usd=7)
    result.speech.append(
        __import__("beepboop.agent", fromlist=["Speech"]).Speech(text="Timer set.")
    )
    result.stages_ms["fastpath"] = 1.5
    await log.finish(start, "set a timer", result)
    ex = await db.read(lambda c: repo.turns.explain(c, start.turn_id))
    assert ex is not None
    assert ex.turn.transcript == "set a timer"
    assert ex.turn.reply == "Timer set."
    assert ex.turn.path == "fast"
    assert ex.turn.cost_micro_usd == 7
    assert ex.turn.stages_ms["fastpath"] == 1.5
    assert ex.turn.id_stage == "final"
    assert [c.tool for c in ex.tool_calls] == ["timer.set"]
    why = explain_turn(ex, names={alex: "Alex"})
    assert "Heard from Alex" in why
    assert "fast path" in why
    assert "allowed by grant g1" in why


async def test_retention_zero_keeps_row_without_words(db: Database) -> None:
    sam = await _profile(db, "Sam")
    await db.write(
        lambda c: c.execute(
            "UPDATE profiles SET transcript_retention_days = 0 WHERE id = ?", (sam,)
        )
    )
    log = DbTurnLog(db)
    tid = "01JBX6Q8Y4H3K9ZR2M7V5T8WDB"
    start = TurnStart(
        tid, _p(sam, tid), "en-US", "kitchen", "s", "t", None, datetime(2026, 11, 3, tzinfo=UTC)
    )
    await log.start(start)
    await log.finish(start, "secret words", TurnResult(tid, "llm"))
    row = await db.read(lambda c: repo.turns.get_turn(c, tid))
    assert row is not None
    assert row.transcript is None
    assert row.reply is None


async def test_previous_not_me_and_forget(db: Database) -> None:
    alex = await _profile(db, "Alex", "admin")
    log = DbTurnLog(db)
    t1, t2 = "01JBX6Q8Y4H3K9ZR2M7V5T8WD1", "01JBX6Q8Y4H3K9ZR2M7V5T8WD2"
    for tid, at in ((t1, 1), (t2, 2)):
        start = TurnStart(
            tid,
            _p(alex, tid),
            "en-US",
            "kitchen",
            "s",
            "t",
            "final",
            datetime(2026, 11, 3, 0, 0, at, tzinfo=UTC),
        )
        await log.start(start)
        await log.finish(start, f"said {tid}", TurnResult(tid, "fast"))
    prev = await log.previous("kitchen", t2)
    assert prev is not None
    assert prev.turn_id == t1
    await log.not_me(t1, corrector=_p(alex, t2), disputed=False)
    row = await db.read(lambda c: repo.turns.get_turn(c, t1))
    assert row is not None
    assert row.flagged
    assert row.corrected_profile_id == "guest"
    audits = await db.read(lambda c: repo.audit.list_(c))
    assert any(a.action == "turn.not_me" for a in audits)
    assert await log.forget(t1)
    row = await db.read(lambda c: repo.turns.get_turn(c, t1))
    assert row is not None
    assert row.transcript is None


async def test_directory_names_and_admin(db: Database) -> None:
    alex = await _profile(db, "Alex", "admin")
    d = DbDirectory(db)
    await d.refresh()
    assert d.name(alex) == "Alex"
    assert d.kind(alex) == "admin"
    assert d.kind("guest") == "guest"
    assert d.admin_name() == "Alex"


async def test_wiring_without_credentials_is_fast_path_only(db: Database) -> None:
    provider = await load_provider(db, None)
    assert isinstance(provider, NoLLM)
    assert provider.reason == "unconfigured"
    await db.write(
        lambda c: repo.household.set_(
            c,
            "llm",
            {
                "providers": {"bedrock": {"credentials": "ambient"}},
                "aliases": {"fast": {"provider": "bedrock", "model": "m1"}},
                "default_alias": "fast",
                "ladder": ["fast"],
            },
        )
    )
    ambient = await load_provider(db, None)
    assert isinstance(ambient, Ladder)
    assert ambient.rungs[0].model_id == "m1"


async def test_build_turn_loop_records_turns(db: Database) -> None:
    alex = await _profile(db, "Alex", "admin")
    registry = Registry(
        policy_state=lambda: __import__("beepboop.policy", fromlist=["PolicyState"]).PolicyState(
            profiles={}
        ),
        approvals=ApprovalService(MemoryApprovalStore(), lambda: "A1"),
        context_factory=Any,  # type: ignore[arg-type]
    )
    speaker = ListSpeaker()
    loop = await build_turn_loop(db=db, registry=registry, secrets=None, speaker=speaker)
    assert isinstance(loop.provider, NoLLM)
    assert isinstance(registry.taint, ConversationTaint)
    tid = "01JBX6Q8Y4H3K9ZR2M7V5T8WDC"
    result = await loop.handle(Utterance("what's the weather", _p(alex, tid)))
    assert result.path == "offline"
    row = await db.read(lambda c: repo.turns.get_turn(c, tid))
    assert row is not None
    assert row.path == "offline"
    assert row.transcript == "what's the weather"
    inputs = await loop.prompt_inputs(Utterance("x", _p(alex, tid)))  # type: ignore[misc]
    assert isinstance(inputs, PromptInputs)
    assert inputs.speaker_name == "Alex"


def test_loop_types_are_importable() -> None:
    assert TurnLoop
    assert FastPath
