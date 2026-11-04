"""WEB_UI §5.9: a ``critical`` call typed into Chat with dry-run off still goes through the
approval flow, and approving the banner executes it (the turn is followed in the background,
as a voice turn would be). IDENTITY_AND_TRUST §3.1: *test as* forces personal/critical
**calls** to dry-run by effective tier."""

from __future__ import annotations

import asyncio

from beepboop.agent import ListSpeaker, MemoryConversation, PromptInputs, TurnLoop, Utterance
from beepboop.api.routers import chat as chat_router
from beepboop.api.testing import Api, register_frontdoor
from beepboop.testing.llm import ScriptedProvider, text, tool_call


def _loop(api: Api, provider: ScriptedProvider) -> TurnLoop:
    def inputs(u: Utterance) -> PromptInputs:
        return PromptInputs(
            voice_contract="",
            persona_block="",
            module_cards="",
            core_tools=[],
            profile_id=u.principal.profile_id,
            local_time="12:00",
        )

    return TurnLoop(
        api.deps.registry,
        ListSpeaker(),
        MemoryConversation(),
        inputs,
        provider=provider,
        clock=api.clock,
        approval_poll_s=0.01,
    )


async def test_chat_critical_executes_after_web_approval(api: Api) -> None:
    door = register_frontdoor(api.deps.registry)
    _alex, admin = await api.person("Alex", "admin")
    api.deps.loop = _loop(api, ScriptedProvider([tool_call("c1", "frontdoor.unlock"), text("ok")]))
    api.use(admin)
    r = await api.post("/api/chat", json={"text": "unlock the door", "dry_run": False})
    assert r.status_code == 200, r.text
    body = r.json()
    approval_id = body["pending_approval_id"]
    assert approval_id is not None
    assert [c["decision"] for c in body["tool_calls"]] == ["confirm"]
    assert door.unlocked == 0
    nonce = api.deps.events.nonce_for(approval_id)
    assert nonce is not None
    r = await api.post(f"/api/approvals/{approval_id}/approve", json={"nonce": nonce})
    assert r.status_code == 200, r.text
    for _ in range(100):  # the background follower re-dispatches with the approval
        if door.unlocked:
            break
        await asyncio.sleep(0.01)
    assert door.unlocked == 1
    await asyncio.gather(*chat_router._FOLLOWERS, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
    record = await api.deps.registry.approvals.store.get(approval_id)
    assert record is not None
    assert record.executed_at is not None


async def test_test_as_forces_dry_run_by_effective_tier(api: Api) -> None:
    """A child cannot reach ``frontdoor.unlock`` (ceiling), so the forced dry-run is
    observed on a read tool as *not* forced and on the critical tool as a denial."""
    door = register_frontdoor(api.deps.registry)
    _alex, admin = await api.person("Alex", "admin")
    robin, _ = await api.person("Robin", "child", login=False)
    api.deps.loop = _loop(api, ScriptedProvider([tool_call("c1", "frontdoor.status"), text("ok")]))
    api.use(admin)
    r = await api.post(
        "/api/chat", json={"text": "status", "dry_run": False, "as_profile_id": robin}
    )
    assert r.status_code == 200, r.text
    chips = r.json()["tool_calls"]
    assert chips
    assert chips[0]["tool"] == "frontdoor.status"
    assert chips[0]["decision"] == "deny"  # no grant for the child: policy, not dry-run
    assert door.dry_runs == []
