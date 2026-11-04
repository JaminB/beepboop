"""The turn loop over a registry with fakes and a scripted LLM (ARCHITECTURE §5, LLM_PROVIDERS)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from beepboop.agent import NoLLM, PromptInputs, deny_phrase
from beepboop.agent.phrases import LOST_CONNECTION, PHRASES, SETUP_NEEDED
from beepboop.policy.approvals import ApprovalRecord
from beepboop.policy.pin import PinState
from beepboop.providers.llm.fallback import AlwaysReachable, Ladder, Rung
from beepboop.providers.llm.protocol import (
    AuthError,
    Done,
    TextDelta,
    Throttled,
    Usage,
)
from beepboop.registry import FIND_TOOLS_SPEC, ListAuditSink, Registry
from beepboop.testing.llm import ScriptedProvider, text, tool_call

# ------------------------------------------------------------------- fast path --


async def test_fast_path_timer_builds_schema_args(make_loop: Any, utter: Any, timers: Any) -> None:
    h = make_loop()
    result = await h.loop.handle(utter("set a timer for ten minutes"))
    assert result.path == "fast"
    assert timers.calls == [("timer.set", {"kind": "timer", "duration_s": 600})]
    assert result.tool_calls[0].decision == "allow"
    assert h.said()[-1].text == "Timer set for 10 minutes."
    assert h.speaker.spoken[-1][2] is True  # last


async def test_fast_path_list_add(make_loop: Any, utter: Any, timers: Any) -> None:
    h = make_loop()
    result = await h.loop.handle(utter("add milk to the shopping list"))
    assert result.path == "fast"
    assert timers.calls == [("list.add", {"items": ["milk"], "list": "shopping"})]


async def test_fast_path_home_resolves_then_dispatches(
    make_loop: Any, utter: Any, house: Any
) -> None:
    """ROADMAP v0.1 #6 command 1, offline: no LLM configured at all."""
    h = make_loop(NoLLM())
    result = await h.loop.handle(utter("Hey beepboop, turn off the kitchen lights"))
    assert result.path == "fast"
    assert house.calls == [("home.turn_off", {"target": {"name": "the kitchen lights"}})]
    assert h.said()[-1].text == "The kitchen lights off."


async def test_fast_path_falls_through_when_unresolved(
    make_loop: Any, utter: Any, house: Any
) -> None:
    h = make_loop()  # NoLLM: the fall-through lands on the offline phrase
    result = await h.loop.handle(utter("turn off the attic fan"))
    assert result.path == "offline"
    assert house.calls == []
    assert h.said()[-1].clip == "offline"


async def test_fast_path_never_runs_above_act(make_loop: Any, utter: Any, house: Any) -> None:
    """§12.2 step 3: a target whose tier is above act goes to the LLM (garage → critical)."""
    h = make_loop()
    h.registry.targets = _garage_targets
    result = await h.loop.handle(utter("close the garage door"))
    assert result.path == "offline"
    assert house.calls == []
    assert h.registry.approvals.store.rows == {}


async def _garage_targets(principal: Any, tool: Any, args: Any) -> list[Any]:
    from beepboop.policy.types import Target

    return [Target("u-garage", "cover", "close_cover", "garage", "critical")]


async def test_fast_path_denial_speaks_phrase(make_loop: Any, utter: Any, timers: Any) -> None:
    h = make_loop()
    result = await h.loop.handle(utter("set a timer for ten minutes", "guest"))
    assert result.tool_calls[0].decision == "deny"
    assert h.said()[-1].clip == "unknown_speaker"
    assert timers.calls == []


async def test_fast_path_child_ceiling_phrase(make_loop: Any, utter: Any) -> None:
    h = make_loop()
    await h.loop.handle(utter("add milk to the shopping list", "mia"))
    said = h.said()[-1]
    assert said.clip == "not_granted"
    assert said.text is not None
    assert "act" not in said.text.split()


async def test_fast_path_is_never_tainted(make_loop: Any, utter: Any, timers: Any) -> None:
    h = make_loop()
    await h.conversation.taint("alex", "living-room")
    result = await h.loop.handle(utter("set a timer for ten minutes"))
    assert result.tool_calls[0].decision == "allow"


async def test_time_and_date_core_actions(make_loop: Any, utter: Any) -> None:
    h = make_loop()
    await h.loop.handle(utter("what time is it"))
    assert h.said()[-1].text == "It's 12:00 PM."
    await h.loop.handle(utter("what's the date", turn_id="T2"))
    assert h.said()[-1].text == "It's Tuesday, November 3."


async def test_not_me_and_forget(make_loop: Any, utter: Any) -> None:
    h = make_loop()
    await h.loop.handle(utter("what time is it", "sam", turn_id="T1"))
    await h.loop.handle(utter("that wasn't me", "alex", turn_id="T2"))
    assert h.log.flags == [("T1", False)]  # an admin corrects: not disputed
    assert h.said()[-1].clip == "not_me"
    await h.loop.handle(utter("that wasn't me", "kim", turn_id="T3"))
    assert h.log.flags[-1] == ("T2", True)  # kim is neither the speaker nor an admin
    await h.loop.handle(utter("forget that", "kim", turn_id="T4"))
    assert h.log.forgotten == ["T3"]
    assert h.said()[-1].clip == "forget"
    await h.loop.handle(utter("forget that", "sam", turn_id="T5"))
    assert h.log.forgotten == ["T3"]  # T4 was kim's, not sam's


async def test_not_me_window_is_five_minutes(make_loop: Any, utter: Any) -> None:
    h = make_loop()
    await h.loop.handle(utter("what time is it", "sam", turn_id="T1"))
    h.clock.advance(301)
    await h.loop.handle(utter("that wasn't me", "sam", turn_id="T2"))
    assert h.log.flags == []


# ----------------------------------------------------------------- no LLM paths --


async def test_offline_without_llm(make_loop: Any, utter: Any) -> None:
    h = make_loop()
    result = await h.loop.handle(utter("what's the weather"))
    assert result.path == "offline"
    assert h.said()[-1].clip == "offline"
    assert h.log.finished[-1][1].path == "offline"


async def test_unconfigured_llm_says_setup(make_loop: Any, utter: Any) -> None:
    h = make_loop(NoLLM("unconfigured"))
    await h.loop.handle(utter("what's the weather"))
    assert h.said()[-1].text == SETUP_NEEDED


async def test_offline_ladder_speaks_offline_without_sending(make_loop: Any, utter: Any) -> None:
    class Down:
        async def reachable(self) -> bool:
            return False

        def note_failure(self) -> None:
            return None

    provider = ScriptedProvider([text("never")])
    h = make_loop(Ladder([Rung("fast", provider, provider.model_id)], Down()))
    result = await h.loop.handle(utter("what's the weather"))
    assert result.path == "offline"
    assert provider.requests == []
    assert h.said()[-1].clip == "offline"


async def test_auth_error_says_setup(make_loop: Any, utter: Any) -> None:
    h = make_loop(ScriptedProvider([AuthError("no keys")]))
    result = await h.loop.handle(utter("what's the weather"))
    assert result.path == "error"
    assert h.said()[-1].text == SETUP_NEEDED


# -------------------------------------------------------------------- llm loop --


async def test_llm_tool_then_chunked_reply(make_loop: Any, utter: Any, registry: Registry) -> None:
    provider = ScriptedProvider(
        [
            tool_call("c1", "demo.echo", {"text": "hi"}),
            text("You said ", "hi. That was easy. Want another?"),
        ]
    )
    h = make_loop(provider)
    result = await h.loop.handle(utter("please echo hi"))
    assert result.path == "llm"
    assert [c.tool for c in result.tool_calls] == ["demo.echo"]
    spoken = h.speaker.spoken
    assert [s.text for _, s, _ in spoken] == ["You said hi.", "That was easy.", "Want another?"]
    assert [last for _, _, last in spoken] == [False, False, True]
    assert spoken[-1][1].listen_after is True  # a question opens the reply window
    assert h.speaker.earcons == [("T1", "ack")]
    second = provider.requests[1]
    assert second.messages[-1].content[0].type == "tool_result"
    assert second.messages[0].content[0].type == "text"
    assert result.llm_provider == "scripted:global.anthropic.claude-haiku-5-5"
    assert result.stages_ms["rung"] == "fast"
    assert result.tokens_in == 200
    assert result.tokens_out == 20
    assert result.cost_micro_usd > 0
    assert "llm_first_sentence" in result.stages_ms


async def test_prompt_layout_has_one_cache_point(make_loop: Any, utter: Any) -> None:
    provider = ScriptedProvider([text("Hi.")])
    h = make_loop(provider)
    await h.loop.handle(utter("hello"))
    req = provider.requests[0]
    assert [b.cache for b in req.system] == [False, False, True, False]
    assert req.system[0].text == h.loop.prompt_inputs(utter("x")).voice_contract
    assert "speaker: Alex (admin)" in req.system[-1].text
    names = [t.name for t in req.tools]
    assert names == sorted(names)
    assert "demo.echo" not in names  # deferred until find_tools loads it


async def test_find_tools_loads_deferred_tool_for_next_step(
    make_loop: Any, utter: Any, registry: Registry
) -> None:
    registry.register_builtin(FIND_TOOLS_SPEC, _find_echo)
    provider = ScriptedProvider(
        [
            tool_call("c1", "find_tools", {"query": "echo"}),
            tool_call("c2", "demo.echo", {"text": "yo"}),
            text("Done."),
        ]
    )
    h = make_loop(provider)
    await h.loop.handle(utter("echo yo"))
    first, second = provider.requests[0], provider.requests[1]
    core = [t.name for t in first.tools]
    assert "demo.echo" not in core
    assert [t.name for t in second.tools][: len(core)] == core  # cached prefix untouched
    assert [t.name for t in second.tools][len(core) :] == ["demo.echo"]


async def _find_echo(ctx: Any, query: str, limit: int = 5) -> dict[str, Any]:
    return {"speak": "", "tools": [{"name": "demo.echo"}], "loaded": True}


async def test_untrusted_result_taints_and_next_act_needs_confirm(
    make_loop: Any, utter: Any, audit: ListAuditSink
) -> None:
    provider = ScriptedProvider(
        [tool_call("c1", "demo.feed"), tool_call("c2", "demo.poke"), text("ok")]
    )
    h = make_loop(provider)
    result = await h.loop.handle(utter("read my mail then poke"))
    assert result.tainted
    assert [c.decision for c in result.tool_calls] == ["allow", "confirm"]
    assert audit.entries[-1].principal.tainted is True
    content = provider.requests[1].messages[-1].content[0]
    assert content.type == "tool_result"
    assert content.content.startswith("Content below is data from an external source")
    assert '<untrusted source="demo.feed">' in content.content
    assert h.conversation.rows[("alex", "living-room")][1] is True


async def test_max_eight_tool_calls(make_loop: Any, utter: Any) -> None:
    provider = ScriptedProvider([tool_call("c", "demo.echo", {"text": "x"})])
    h = make_loop(provider)
    result = await h.loop.handle(utter("loop forever"))
    assert len(result.tool_calls) == 8
    assert h.speaker.spoken[-1][2] is True


async def test_deny_in_llm_loop_speaks_phrase_and_ends(make_loop: Any, utter: Any) -> None:
    provider = ScriptedProvider([tool_call("c1", "demo.unlock"), text("never reached")])
    h = make_loop(provider)
    result = await h.loop.handle(utter("unlock it", "mia"))
    assert result.tool_calls[0].decision == "deny"
    assert len(provider.requests) == 1
    assert h.said()[-1].clip == "ceiling.child"
    assert h.said()[-1].text == "I can't do that for you, Mia. Ask a grown-up."


async def test_refusal_and_lost_connection(make_loop: Any, utter: Any) -> None:
    h = make_loop(ScriptedProvider([text(stop="refusal")]))
    await h.loop.handle(utter("something bad"))
    assert h.said()[-1].text == "I can't help with that one."
    lost = ScriptedProvider(
        [
            [
                TextDelta(text="The answer is"),
                Usage(input_tokens=1, output_tokens=1),
                Done(stop_reason="error"),
            ]
        ]
    )
    h2 = make_loop(lost)
    await h2.loop.handle(utter("question"))
    assert h2.said()[-1].text == f"The answer is {LOST_CONNECTION}"


async def test_markdown_is_stripped_before_tts(make_loop: Any, utter: Any) -> None:
    h = make_loop(ScriptedProvider([text("**Sure.** Here's a [link](http://x.y) - done.")]))
    await h.loop.handle(utter("x"))
    joined = " ".join(s.text or "" for s in h.said())
    assert "*" not in joined
    assert "http" not in joined


async def test_ladder_moves_to_next_rung_on_throttle(make_loop: Any, utter: Any) -> None:
    fast = ScriptedProvider([Throttled("slow down")], model_id="global.anthropic.claude-haiku-5-5")
    smart = ScriptedProvider([text("From smart.")], model_id="global.anthropic.claude-sonnet-5-5")
    ladder = Ladder(
        [Rung("fast", fast, fast.model_id), Rung("smart", smart, smart.model_id)], AlwaysReachable()
    )
    h = make_loop(ladder)
    result = await h.loop.handle(utter("hi"))
    assert result.rung == "smart"
    assert result.llm_provider == "scripted:global.anthropic.claude-sonnet-5-5"
    assert h.said()[-1].text == "From smart."


async def test_cancellation_closes_the_stream(make_loop: Any, utter: Any) -> None:
    provider = ScriptedProvider(
        [
            [
                TextDelta(text="Long "),
                TextDelta(text="story"),
                Usage(input_tokens=1, output_tokens=1),
                Done(stop_reason="end_turn"),
            ]
        ],
        hang_after=1,
    )
    h = make_loop(provider)
    task = asyncio.create_task(h.loop.handle(utter("tell me a story")))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled == 1
    assert h.log.finished[-1][1].cancelled is True


# ------------------------------------------------------------- conversation window --


async def test_conversation_continues_for_same_profile_only(make_loop: Any, utter: Any) -> None:
    provider = ScriptedProvider([text("One.")])
    h = make_loop(provider)
    await h.loop.handle(utter("first", "alex", turn_id="T1"))
    await h.loop.handle(utter("second", "alex", turn_id="T2"))
    assert [m.role for m in provider.requests[1].messages] == ["user", "assistant", "user"]
    assert "conversation: continuing" in provider.requests[1].system[-1].text
    await h.loop.handle(utter("third", "sam", turn_id="T3"))
    assert len(provider.requests[2].messages) == 1  # anyone else starts fresh
    await h.loop.handle(utter("fourth", "alex", turn_id="T4"))
    assert len(provider.requests[3].messages) == 1  # sam spoke in between


async def test_guest_never_continues(make_loop: Any, utter: Any) -> None:
    provider = ScriptedProvider([text("Hi.")])
    h = make_loop(provider)
    await h.loop.handle(utter("one", "guest", turn_id="T1"))
    await h.loop.handle(utter("two", "guest", turn_id="T2"))
    assert len(provider.requests[1].messages) == 1


# --------------------------------------------------------------------- approvals --


async def test_critical_pends_then_web_approval_executes(
    make_loop: Any, utter: Any, registry: Registry
) -> None:
    provider = ScriptedProvider([tool_call("c1", "demo.unlock"), text("never")])
    h = make_loop(provider)
    u = utter("unlock it")
    result = await h.loop.handle(u)
    assert result.pending is not None
    assert len(provider.requests) == 1  # a confirm pauses the whole step
    prompt = h.said()[-1]
    assert prompt.clip == "approval.prompt"
    assert prompt.text == "To unlock, approve on your phone."
    follow = asyncio.create_task(h.loop.follow_approval(result, u))
    await asyncio.sleep(0.02)
    store = registry.approvals.store
    assert await store.decide(result.pending.approval_id, "alex", "approved", "web", h.clock())
    h.loop.notify_approval(result.pending.approval_id)
    await asyncio.wait_for(follow, 1)
    assert h.said()[-1].clip == "approval.done"
    assert h.said()[-1].text == "Done. Unlocked."
    rec = await store.get(result.pending.approval_id)
    assert isinstance(rec, ApprovalRecord)
    assert rec.executed_at is not None


async def test_web_denial_and_expiry(make_loop: Any, utter: Any, registry: Registry) -> None:
    h = make_loop(ScriptedProvider([tool_call("c1", "demo.unlock")]))
    u = utter("unlock it")
    result = await h.loop.handle(u)
    assert result.pending is not None
    await registry.approvals.store.decide(
        result.pending.approval_id, "alex", "denied", "web", h.clock()
    )
    await asyncio.wait_for(h.loop.follow_approval(result, u), 1)
    assert h.said()[-1].clip == "approval.denied"

    u2 = utter("unlock it again", turn_id="T2")
    result2 = await h.loop.handle(u2)
    follow = asyncio.create_task(h.loop.follow_approval(result2, u2))
    await asyncio.sleep(0.02)
    h.clock.advance(61)
    await asyncio.wait_for(follow, 1)
    assert h.said()[-1].clip == "approval.expired"


async def test_pin_flow_ok_executes(make_loop: Any, utter: Any) -> None:
    pins = _pins({"sam": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"})
    h = make_loop(ScriptedProvider([tool_call("c1", "demo.unlock")]), pins=pins)
    u = utter("unlock it", "sam")
    result = await h.loop.handle(u)
    assert h.said()[-1].clip == "approval.prompt_pin"
    assert h.control.pins
    assert h.control.pins[0][1] == result.pending.approval_id
    assert h.control.pins[0][3] == PHRASES["pin.prompt"]
    follow = asyncio.create_task(h.loop.follow_approval(result, u))
    await asyncio.sleep(0.01)
    assert h.loop.deliver_pin_result(result.pending.approval_id, "ok")
    await asyncio.wait_for(follow, 1)
    assert h.said()[-1].clip == "approval.done"
    rec = await h.registry.approvals.store.get(result.pending.approval_id)
    assert rec is not None
    assert rec.factor == "pin"


async def test_pin_mismatch_keeps_phone_path_and_locks_after_three(
    make_loop: Any, utter: Any
) -> None:
    pins = _pins({"sam": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"})
    h = make_loop(ScriptedProvider([tool_call("c1", "demo.unlock")]), pins=pins)
    for i in range(3):
        u = utter("unlock it", "sam", turn_id=f"T{i}")
        result = await h.loop.handle(u)
        follow = asyncio.create_task(h.loop.follow_approval(result, u))
        await asyncio.sleep(0.01)
        h.loop.deliver_pin_result(result.pending.approval_id, "mismatch")
        await asyncio.sleep(0.02)
        h.clock.advance(61)
        await asyncio.wait_for(follow, 1)
    clips = [s.clip for s in h.said()]
    assert clips.count("pin.mismatch") == 2
    assert "pin.locked" in clips
    assert pins.states["sam"].locked_until is not None
    assert h.log.flagged == ["T2"]  # IDENTITY_AND_TRUST §11: a PIN lockout flags the turn


async def test_no_login_profile_gets_no_login_phrase_then_pin(make_loop: Any, utter: Any) -> None:
    pins = _pins({"kim": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"})
    h = make_loop(ScriptedProvider([tool_call("c1", "demo.unlock")]), pins=pins)
    await h.loop.handle(utter("unlock it", "kim"))
    said = h.said()[-1]
    assert said.clip == "approval.no_login"
    assert said.text == PHRASES["approval.no_login"].format(admin="Alex")
    assert len(h.control.pins) == 1


async def test_turn_log_records_the_turn(make_loop: Any, utter: Any) -> None:
    h = make_loop(ScriptedProvider([text("Hi there.")]))
    await h.loop.handle(utter("hello"))
    transcript, result = h.log.finished[-1]
    assert transcript == "hello"
    assert result.reply_text == "Hi there."
    assert h.log.started[-1].principal.profile_id == "alex"


def test_deny_phrase_rules() -> None:
    assert deny_phrase("ceiling", "child") == "ceiling.child"
    assert deny_phrase("ceiling", "member") == "not_granted"
    assert deny_phrase("no_grant", "guest") == "unknown_speaker"
    assert deny_phrase("no_factor", "guest") == "unknown_speaker"


def test_prompt_inputs_type_is_reexported() -> None:
    assert PromptInputs.__name__ == "PromptInputs"


def _pins(hashes: dict[str, str]) -> Any:
    class Pins:
        def __init__(self) -> None:
            self.states: dict[str, PinState] = {k: PinState(enabled=True) for k in hashes}

        async def load(self, profile_id: str) -> tuple[str | None, PinState]:
            return hashes.get(profile_id), self.states.get(profile_id, PinState(enabled=False))

        async def save(self, profile_id: str, state: PinState) -> None:
            self.states[profile_id] = state

    return Pins()


async def test_tool_spans_and_first_sentence_are_recorded(make_loop: Any, utter: Any) -> None:
    """OPERATIONS §11: one ``tool.<name>`` span per call with risk and decision, and
    ``llm_first_sentence`` measured from the request."""
    provider = ScriptedProvider(
        [tool_call("c1", "demo.echo", {"text": "a"}), tool_call("c2", "demo.unlock"), text("Hi.")]
    )
    h = make_loop(provider)
    result = await h.loop.handle(utter("echo then unlock"))
    echo = result.stages_ms["tool.demo.echo"]
    assert isinstance(echo, dict)
    assert echo["risk"] == "read"
    assert echo["decision"] == "allow"
    assert isinstance(echo["ms"], float)
    unlock = result.stages_ms["tool.demo.unlock"]
    assert isinstance(unlock, dict)
    assert unlock["decision"] == "confirm"
    assert unlock["factor"] == "web"
    assert "llm_first_sentence" not in result.stages_ms  # the confirm phrase is not a sentence
    result = await h.loop.handle(utter("say hi", turn_id="T9"))
    assert "llm_first_sentence" in result.stages_ms


async def test_guest_window_is_never_saved(make_loop: Any, utter: Any) -> None:
    """DATA_AND_PRIVACY §8: no conversation window beyond the turn for a guest."""
    h = make_loop(ScriptedProvider([tool_call("c1", "demo.echo", {"text": "a"}), text("ok")]))
    await h.loop.handle(utter("echo", "guest"))
    assert ("guest", "living-room") not in h.conversation.rows
