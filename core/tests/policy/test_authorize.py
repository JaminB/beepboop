"""``authorize()`` against the worked examples in IDENTITY_AND_TRUST §7.3 and the §7.2 rules."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from beepboop.policy import (
    Approval,
    PolicyState,
    RateLimiter,
    SimpleTool,
    Target,
    args_digest,
    authorize,
)

from .conftest import NOW, voice, web

TURN_OFF = SimpleTool("home.turn_off", "act")
STATE = SimpleTool("home.state", "read")
SET = SimpleTool("home.set", "act")
CALDAV = SimpleTool("caldav.events", "personal")
TIMER = SimpleTool("timer.set", "act")
LIGHT = Target("kitchen-light", "light", "turn_off")
UNLOCK = Target("lock-uuid", "lock", "unlock")


def test_kitchen_lights_allowed_for_sam(state: PolicyState) -> None:
    d = authorize(state, voice("sam", 0.82, 0.21), TURN_OFF, {}, [LIGHT], NOW)
    assert d.outcome == "allow"
    assert d.effective_risk == "act"
    assert d.grant_id == "g1"


def test_guest_without_grant_is_denied_no_grant(state: PolicyState) -> None:
    d = authorize(state, voice("guest"), TURN_OFF, {}, [LIGHT], NOW)
    assert (d.outcome, d.reason) == ("deny", "no_grant")


def test_guest_may_read_state_and_set_timers(state: PolicyState) -> None:
    assert authorize(state, voice("guest"), STATE, {}, [], NOW).outcome == "allow"
    assert authorize(state, voice("guest"), TIMER, {}, [], NOW).outcome == "allow"


def test_calendar_allowed_above_strict(state: PolicyState) -> None:
    assert authorize(state, voice("alex", 0.91, 0.25), CALDAV, {}, [], NOW).outcome == "allow"


def test_calendar_below_strict_needs_factor(state: PolicyState) -> None:
    d = authorize(state, voice("alex", 0.71, 0.06), CALDAV, {}, [], NOW)
    assert d.outcome == "confirm"
    assert d.factors == ("web",)


def test_unlock_is_critical_and_confirms_for_adult(state: PolicyState) -> None:
    d = authorize(state, voice("alex", 0.95, 0.30), SET, {}, [UNLOCK], NOW)
    assert d.outcome == "confirm"
    assert d.effective_risk == "critical"


def test_child_hits_ceiling(state: PolicyState) -> None:
    d = authorize(state, voice("mia"), SET, {}, [UNLOCK], NOW)
    assert (d.outcome, d.reason, d.effective_risk) == ("deny", "ceiling", "critical")


def test_sam_lacks_entity_grant_for_nothing_but_lock_is_adults(state: PolicyState) -> None:
    # sam is an adult: the adults grant covers the lock, so critical -> confirm, not deny.
    assert authorize(state, voice("sam"), SET, {}, [UNLOCK], NOW).outcome == "confirm"


def test_tainted_turn_confirms_every_non_read_call(state: PolicyState) -> None:
    assert (
        authorize(state, voice("alex", tainted=True), TURN_OFF, {}, [LIGHT], NOW).outcome
        == "confirm"
    )
    assert authorize(state, voice("alex", tainted=True), STATE, {}, [], NOW).outcome == "allow"


def test_tainted_removes_pin_factor(state: PolicyState) -> None:
    pinned = replace(state.profiles["alex"], pin_enabled=True)
    s = PolicyState({**state.profiles, "alex": pinned}, state.grants, state.entities)
    assert authorize(s, voice("alex"), SET, {}, [UNLOCK], NOW).factors == ("web", "pin")
    assert authorize(s, voice("alex", tainted=True), SET, {}, [UNLOCK], NOW).factors == ("web",)


def test_test_as_adult_is_impersonation(state: PolicyState) -> None:
    d = authorize(state, web("sam", actor="alex"), STATE, {}, [], NOW)
    assert (d.outcome, d.reason) == ("deny", "impersonation")


def test_test_as_child_is_evaluated_as_the_child(state: PolicyState) -> None:
    assert authorize(state, web("mia", actor="alex"), TURN_OFF, {}, [LIGHT], NOW).outcome == "allow"


def test_new_entity_unreachable_until_granted(state: PolicyState) -> None:
    new = Target("new-uuid", "light", "turn_on")
    for who in ("alex", "sam", "mia", "guest"):
        d = authorize(state, voice(who), TURN_OFF, {}, [new], NOW)
        assert d.outcome == "deny", who
        assert d.reason in ("no_grant", "ceiling")


def test_blocked_service_is_denied_for_everyone(state: PolicyState) -> None:
    blocked = Target("kitchen-light", "homeassistant", "turn_off")
    d = authorize(state, voice("alex"), TURN_OFF, {}, [blocked], NOW)
    assert (d.outcome, d.reason) == ("deny", "service_blocked")


def test_disabled_profile(state: PolicyState) -> None:
    disabled = replace(state.profiles["sam"], disabled=True)
    s = PolicyState({**state.profiles, "sam": disabled}, state.grants, state.entities)
    assert authorize(s, voice("sam"), STATE, {}, [], NOW).reason == "disabled"


def test_unknown_profile_is_disabled(state: PolicyState) -> None:
    assert authorize(state, voice("nobody"), STATE, {}, [], NOW).reason == "disabled"


def test_act_below_tau_act_is_identity_deny(state: PolicyState) -> None:
    d = authorize(state, voice("sam", 0.5, 0.05), TURN_OFF, {}, [LIGHT], NOW)
    assert (d.outcome, d.reason) == ("deny", "identity")


def test_external_scope_caps(state: PolicyState) -> None:
    p = replace(voice("alex"), via="pat", scopes=frozenset({"home.read"}))
    d = authorize(state, p, TURN_OFF, {}, [LIGHT], NOW)
    assert (d.outcome, d.reason) == ("deny", "scope")


def test_rate_limit(state: PolicyState) -> None:
    rl = RateLimiter()
    s = PolicyState(state.profiles, state.grants, state.entities, rate_limiter=rl)
    for _ in range(20):
        rl.record("sam", "living-room", "home.turn_off", NOW)
    d = authorize(s, voice("sam"), TURN_OFF, {}, [LIGHT], NOW)
    assert d.reason == "rate_limited"
    later = NOW + timedelta(seconds=61)
    assert authorize(s, voice("sam"), TURN_OFF, {}, [LIGHT], later).outcome == "allow"


def test_naive_now_rejected(state: PolicyState) -> None:
    with pytest.raises(ValueError, match="aware"):
        authorize(state, voice("sam"), STATE, {}, [], NOW.replace(tzinfo=None))


# ------------------------------------------------------------- re-dispatch --


def _approval(**overrides: object) -> Approval:
    base: dict[str, object] = {
        "id": "A1",
        "profile_id": "alex",
        "tool": "home.set",
        "args_digest": args_digest("home.set", {"x": 1}, ["lock-uuid"]),
        "exp": NOW + timedelta(seconds=30),
        "decision": "approved",
        "executed_at": None,
    }
    base.update(overrides)
    return Approval(**base)  # pyright: ignore[reportArgumentType]


def test_redispatch_with_valid_approval_allows(state: PolicyState) -> None:
    d = authorize(state, voice("alex"), SET, {"x": 1}, [UNLOCK], NOW, approval=_approval())
    assert d.outcome == "allow"


@pytest.mark.parametrize(
    "bad",
    [
        {"decision": None},
        {"decision": "denied"},
        {"executed_at": NOW},
        {"exp": NOW - timedelta(seconds=1)},
        {"profile_id": "sam"},
        {"tool": "home.turn_on"},
        {"args_digest": "nope"},
    ],
)
def test_redispatch_with_bad_approval_denies(state: PolicyState, bad: dict[str, object]) -> None:
    d = authorize(state, voice("alex"), SET, {"x": 1}, [UNLOCK], NOW, approval=_approval(**bad))
    assert (d.outcome, d.reason) == ("deny", "approval_invalid")


def test_grant_revoked_in_window_wins_over_approval(state: PolicyState) -> None:
    from beepboop.policy import GrantIndex

    without_lock = GrantIndex.build([g for g in state.grants.all() if g.id != "g3"])
    s = PolicyState(state.profiles, without_lock, state.entities)
    d = authorize(s, voice("alex"), SET, {"x": 1}, [UNLOCK], NOW, approval=_approval())
    assert (d.outcome, d.reason) == ("deny", "no_grant")
