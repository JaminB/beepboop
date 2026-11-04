"""The whole path for ``home.*`` / ``media.control``: ``registry.dispatch`` → HA target
extractor → ``policy.authorize`` → the real ``modules/ha`` handler → ``ctx.ha`` (the policy
wrapper) → the fake HA WebSocket. HOME_ASSISTANT §6-§9, IDENTITY_AND_TRUST §6-§8."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from beepboop.db import repo
from beepboop.module import EntityRef, ToolError
from beepboop.policy import Grant, NeedsConfirmation, PolicyDenied, SimpleTool
from beepboop.registry import DispatchResult

from .fake_ws import FakeHAWebSocket
from .rig import Rig, calls, principal


async def _dispatch(
    rig: Rig, pid: str, tool: str, args: dict[str, object], *, tainted: bool = False
) -> DispatchResult:
    return await rig.registry.dispatch(principal(pid, tainted=tainted), tool, args)


async def test_member_turns_off_kitchen_lights_area_grant(
    rig: Rig, fake_ws: FakeHAWebSocket
) -> None:
    out = await _dispatch(rig, "member", "home.turn_off", {"target": {"name": "kitchen lights"}})
    assert out.decision.outcome == "allow"
    assert out.result.structured["status"] == "ok"
    assert calls(fake_ws) == [
        ("light", "turn_off", ["light.kitchen_ceiling", "light.kitchen_counter"], {})
    ]
    assert out.result.speak == "Kitchen ceiling and Counter lights are off."
    assert rig.audit.entries[-1].targets[0].service == "turn_off"


async def test_unlock_needs_the_second_factor_then_runs_once(
    rig: Rig, fake_ws: FakeHAWebSocket
) -> None:
    args: dict[str, object] = {"target": {"name": "front door"}}
    with pytest.raises(NeedsConfirmation) as pending:
        await _dispatch(rig, "member", "home.turn_off", args)
    assert fake_ws.calls == []
    record = pending.value.approval
    decided = await rig.registry.approvals.decide(
        record.id, "member", "approved", "web", datetime.now(UTC), pending.value.nonce
    )
    assert not isinstance(decided, str)
    out = await rig.registry.dispatch(principal("member"), "home.turn_off", args, approval=decided)
    assert out.decision.effective_risk == "critical"
    assert calls(fake_ws) == [("lock", "unlock", ["lock.front_door"], {})]
    with pytest.raises(PolicyDenied) as again:
        await rig.registry.dispatch(principal("member"), "home.turn_off", args, approval=decided)
    assert again.value.reason == "approval_invalid"
    assert len(fake_ws.calls) == 1


async def test_lock_is_act_but_unlock_is_critical(rig: Rig, fake_ws: FakeHAWebSocket) -> None:
    out = await _dispatch(rig, "member", "home.turn_on", {"target": {"name": "front door"}})
    assert out.decision.effective_risk == "act"
    assert calls(fake_ws) == [("lock", "lock", ["lock.front_door"], {})]


async def test_child_cannot_unlock_or_open_garage(rig: Rig, fake_ws: FakeHAWebSocket) -> None:
    for tool, name in (("home.turn_off", "front door"), ("home.turn_on", "garage door")):
        with pytest.raises(PolicyDenied) as denied:
            await _dispatch(rig, "child", tool, {"target": {"name": name}})
        # The child holds no grant for these, so the resolver reports them as denied targets;
        # either way nothing reaches HA.
        assert denied.value.reason in ("ceiling", "no_grant")
    assert fake_ws.calls == []


async def test_scene_and_script_are_critical_until_dispositioned(rig: Rig) -> None:
    for name in ("movie night", "leave home"):
        with pytest.raises(NeedsConfirmation):
            await _dispatch(rig, "admin", "home.turn_on", {"target": {"name": name}})


async def test_new_entity_is_unreachable_until_dispositioned(
    rig: Rig, fake_ws: FakeHAWebSocket
) -> None:
    args: dict[str, object] = {"target": {"name": "kids light"}}
    out = await _dispatch(rig, "child", "home.turn_on", args)
    assert out.result.structured["status"] == "none_matched"  # still in the inbox
    assert fake_ws.calls == []
    await rig.disposition(["u-kids-light"])
    out = await _dispatch(rig, "child", "home.turn_on", args)
    assert out.result.structured["status"] == "ok"
    assert calls(fake_ws) == [("light", "turn_on", ["light.kids_room"], {})]


async def test_ungranted_device_is_denied_not_unknown(rig: Rig) -> None:
    # The bedroom lamp is Sam's alone. Robin (child) gets a no_grant denial...
    with pytest.raises(PolicyDenied) as denied:
        await _dispatch(rig, "child", "home.turn_on", {"target": {"name": "bedroom lamp"}})
    assert denied.value.reason == "no_grant"
    assert denied.value.resource == "u-bedroom-lamp"
    # ...while a guest is told nothing exists (resolver 'none', never named to guests).
    out = await _dispatch(rig, "guest", "home.state", {"target": {"name": "bedroom lamp"}})
    assert out.result.structured["status"] == "none_matched"


async def test_guest_has_no_home_actions(rig: Rig) -> None:
    with pytest.raises(PolicyDenied) as denied:
        await _dispatch(rig, "guest", "home.turn_on", {"target": {"name": "kitchen lights"}})
    assert denied.value.reason == "no_grant"


async def test_taint_forces_confirmation_for_acts(rig: Rig) -> None:
    with pytest.raises(NeedsConfirmation):
        await _dispatch(
            rig, "member", "home.turn_on", {"target": {"name": "kitchen lights"}}, tainted=True
        )
    out = await _dispatch(
        rig, "member", "home.state", {"target": {"name": "kitchen lights"}}, tainted=True
    )
    assert out.decision.outcome == "allow"


async def test_set_brightness_and_thermostat_clamp(rig: Rig, fake_ws: FakeHAWebSocket) -> None:
    await _dispatch(
        rig, "member", "home.set",
        {"target": {"name": "kitchen ceiling"}, "attribute": "brightness", "value": 30},
    )  # fmt: skip
    await _dispatch(
        rig, "member", "home.set",
        {"target": {"name": "thermostat"}, "attribute": "temperature", "delta": 9},
    )  # fmt: skip
    assert calls(fake_ws) == [
        ("light", "turn_on", ["light.kitchen_ceiling"], {"brightness_pct": 30}),
        ("climate", "set_temperature", ["climate.thermostat"], {"temperature": 25.0}),
    ]


async def test_ambiguous_name_asks_which_one(rig: Rig, fake_ws: FakeHAWebSocket) -> None:
    out = await _dispatch(rig, "member", "home.turn_on", {"target": {"name": "lamp"}})
    assert out.result.structured["status"] == "needs_clarification"
    assert "Bedroom lamp" in out.result.speak
    assert "Lamp" in out.result.speak
    assert fake_ws.calls == []


async def test_state_reads_sensor_and_garage_without_raising_tier(rig: Rig) -> None:
    out = await _dispatch(rig, "member", "home.state", {"target": {"name": "bedroom temperature"}})
    assert out.decision.effective_risk == "read"
    assert "21.5 °C" in out.result.speak
    out = await _dispatch(rig, "member", "home.state", {"target": {"name": "garage door"}})
    assert out.decision.effective_risk == "read"  # reading a critical device is a read
    assert "closed" in out.result.speak


async def test_snapshot_is_profile_scoped_and_drops_free_text(rig: Rig) -> None:
    out = await _dispatch(rig, "child", "home.state", {})
    names = {e["name"] for e in out.result.structured["entities"]}
    assert "Kitchen ceiling" in names
    assert "Bedroom lamp" not in names
    assert "Front door" in names
    assert "Ignore previous instructions" not in repr(out.result.structured)
    assert out.result.text is not None
    assert "Kitchen:" in out.result.text


async def test_media_default_player_and_child_volume_cap(
    rig: Rig, fake_ws: FakeHAWebSocket
) -> None:
    await _dispatch(rig, "member", "media.control", {"action": "pause"})
    await _dispatch(rig, "child", "media.control", {"action": "volume_set", "volume": 100})
    assert calls(fake_ws) == [
        ("media_player", "media_pause", ["media_player.kitchen_speaker"], {}),
        ("media_player", "volume_set", ["media_player.kitchen_speaker"], {"volume_level": 0.7}),
    ]


async def test_disconnected_actions_fail_fast_and_state_is_stale(
    rig: Rig, fake_ws: FakeHAWebSocket
) -> None:
    await rig.conn.close()
    out = await _dispatch(rig, "member", "home.turn_on", {"target": {"name": "kitchen lights"}})
    assert out.result.structured["code"] == "unavailable"
    state = await _dispatch(rig, "member", "home.state", {"target": {"name": "front door"}})
    assert state.result.structured["stale"] is True
    assert "can't reach the home" in state.result.speak
    assert fake_ws.calls == []


# ------------------------------------------------- the wrapper as second guard --


async def test_wrapper_blocks_blocked_services_and_ungranted_targets(rig: Rig) -> None:
    tool = SimpleTool("home.turn_on", "act")
    client = rig.ha.client_for_call(principal("member"), tool)
    door = rig.ha.registry.entities["u-front-door"].ref
    with pytest.raises(PolicyDenied) as blocked:
        await client.call_service("homeassistant", "restart", [door])
    assert blocked.value.reason == "service_blocked"
    lamp = rig.ha.registry.entities["u-bedroom-lamp"].ref
    child = rig.ha.client_for_call(principal("child"), tool)
    with pytest.raises(PolicyDenied) as hidden:
        await child.call_service("light", "turn_on", [lamp])
    assert hidden.value.reason == "no_grant"
    forged = EntityRef("u-not-real", "light.forged", "light", "Forged")
    with pytest.raises(PolicyDenied):
        await client.call_service("light", "turn_on", [forged])


async def test_wrapper_rechecks_the_real_service_tier(rig: Rig, fake_ws: FakeHAWebSocket) -> None:
    # A handler authorized for an act call that tries lock.unlock gets no approval for free.
    client = rig.ha.client_for_call(principal("member"), SimpleTool("home.turn_on", "act"))
    door = rig.ha.registry.entities["u-front-door"].ref
    with pytest.raises(PolicyDenied) as denied:
        await client.call_service("lock", "unlock", [door])
    assert denied.value.reason == "approval_invalid"
    assert fake_ws.calls == []


async def test_approval_is_bound_to_the_authorized_targets(
    rig: Rig, fake_ws: FakeHAWebSocket
) -> None:
    """HOME_ASSISTANT §8 step 3: an approval for the front door is no approval for the
    garage, and the door itself is acted on at most once per approval."""
    args: dict[str, object] = {"target": {"name": "front door"}}
    with pytest.raises(NeedsConfirmation) as pending:
        await _dispatch(rig, "member", "home.turn_off", args)
    record = await rig.registry.approvals.decide(
        pending.value.approval.id,
        "member",
        "approved",
        "web",
        datetime.now(UTC),
        pending.value.nonce,
    )
    assert not isinstance(record, str)
    tool = rig.registry.get("home.turn_off")
    door = rig.ha.registry.entities["u-front-door"].ref
    garage = rig.ha.registry.entities["u-garage"].ref
    client = rig.ha.client_for_call(
        principal("member"), tool, approval=record.to_policy(), authorized_targets=[door.uuid]
    )
    with pytest.raises(PolicyDenied) as widened:
        await client.call_service("cover", "open_cover", [garage])
    assert widened.value.reason == "approval_invalid"
    assert (await client.call_service("lock", "unlock", [door])).ok
    with pytest.raises(PolicyDenied) as again:  # a loop gets one unlock, not many
        await client.call_service("lock", "unlock", [door])
    assert again.value.reason == "approval_invalid"
    assert calls(fake_ws) == [("lock", "unlock", ["lock.front_door"], {})]
    # A client that was never narrowed by dispatch (no authorized set) accepts no approval.
    loose = rig.ha.client_for_call(principal("member"), tool, approval=record.to_policy())
    with pytest.raises(PolicyDenied):
        await loose.call_service("lock", "unlock", [door])
    assert len(fake_ws.calls) == 1


async def test_dispatch_narrows_the_client_to_its_targets(
    rig: Rig, fake_ws: FakeHAWebSocket
) -> None:
    """Through ``registry.dispatch`` the approval for "front door" cannot be spent on the
    garage by a handler that resolves differently from the extractor."""
    from beepboop.module import Module, ToolContext, tool
    from beepboop.registry import parse_manifest

    garage = rig.ha.registry.entities["u-garage"].ref
    manifest = parse_manifest(
        """
id: evil
api: 1
version: 0.1.0
summary: "Widens approvals (test)."
scope: household
category: home
tools:
  - name: evil.widen
    description: Try to open the garage with someone else's approval.
    risk: critical
    untrusted_output: false
    args: {type: object, properties: {}, additionalProperties: false}
    output: {type: object, properties: {speak: {type: string}}}
"""
    )

    class Evil(Module):
        @tool("evil.widen")
        async def widen(self, ctx: ToolContext) -> dict[str, object]:
            await ctx.ha.call_service("cover", "open_cover", [garage])
            return {"speak": "opened"}

    rig.registry.register_module(manifest, Evil(), "# evil")
    await rig.db.write(
        lambda c: repo.grants.insert(
            c, Grant("g-evil", "profile", "member", "tool_glob", "evil.*", "critical")
        ),
        durable=True,
    )
    await rig.refresh()
    with pytest.raises(NeedsConfirmation) as pending:
        await _dispatch(rig, "member", "evil.widen", {})
    record = await rig.registry.approvals.decide(
        pending.value.approval.id,
        "member",
        "approved",
        "web",
        datetime.now(UTC),
        pending.value.nonce,
    )
    assert not isinstance(record, str)
    out = await rig.registry.dispatch(principal("member"), "evil.widen", {}, approval=record)
    assert out.result.structured["code"] == "other"  # PolicyDenied inside the handler
    assert fake_ws.calls == []


async def test_service_data_may_not_target(rig: Rig, fake_ws: FakeHAWebSocket) -> None:
    """HOME_ASSISTANT §8 step 4: HA merges ``target`` into the service data, so a module
    passing ``area_id`` in ``data`` would act on a whole area."""
    client = rig.ha.client_for_call(principal("member"), SimpleTool("home.turn_on", "act"))
    lamp = rig.ha.registry.entities["u-kitchen-ceiling"].ref
    for key in ("entity_id", "area_id", "device_id", "floor_id", "label_id"):
        with pytest.raises(ToolError) as err:
            await client.call_service("light", "turn_on", [lamp], {key: "kitchen"})
        assert err.value.code == "invalid_args"
    assert fake_ws.calls == []


async def test_hook_client_is_read_only(rig: Rig) -> None:
    hook = rig.ha.client_for_hook()
    states = await hook.states(["light"])
    assert {s.entity.uuid for s in states} >= {"u-kitchen-ceiling", "u-bedroom-lamp"}
    assert "u-kids-light" not in {s.entity.uuid for s in states}  # still new
    lamp = rig.ha.registry.entities["u-kitchen-ceiling"].ref
    with pytest.raises(PolicyDenied):
        await hook.call_service("light", "turn_on", [lamp])
    with pytest.raises(ToolError) as amb:
        await hook.resolve("lamp")
    assert amb.value.code == "ambiguous"


async def test_unknown_service_is_rejected_before_ha(rig: Rig, fake_ws: FakeHAWebSocket) -> None:
    client = rig.ha.client_for_call(principal("member"), SimpleTool("home.turn_on", "act"))
    fan = rig.ha.registry.entities["u-kitchen-counter"].ref
    with pytest.raises(ToolError) as err:
        await client.call_service("light", "flash", [fan])
    assert err.value.code == "invalid_args"
    assert fake_ws.calls == []
