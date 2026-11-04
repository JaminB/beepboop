"""Property tests for ROADMAP v0.1 acceptance #4 (IDENTITY_AND_TRUST §7.2 invariants).

* a ``critical`` call never comes back ``allow`` without a consumed-able approval;
* a tainted call above ``read`` never comes back ``allow`` without an approval;
* ``effective_risk`` never exceeds the profile's ceiling on ``allow``/``confirm``;
* a ``new`` HA entity is unreachable by every profile;
* the tier is only ever raised, never lowered;
* deny reasons are from the fixed vocabulary; decisions are deterministic.
"""

from __future__ import annotations

from datetime import timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from beepboop.policy import (
    DENY_REASONS,
    TIER,
    Approval,
    Decision,
    GrantIndex,
    PolicyState,
    Principal,
    SimpleTool,
    Target,
    args_digest,
    authorize,
    effective_risk,
    load_service_table,
)

from .conftest import ENTITIES, GRANTS, NOW, PROFILES, principals, targets, tools

STATE = PolicyState(profiles=PROFILES, grants=GrantIndex.build(GRANTS), entities=ENTITIES)
SERVICES = load_service_table()


def _decide(
    p: Principal, tool: SimpleTool, ts: list[Target], approval: Approval | None = None
) -> Decision:
    return authorize(STATE, p, tool, {"a": 1}, ts, NOW, approval=approval)


@settings(max_examples=400)
@given(p=principals, tool=tools, ts=targets)
def test_critical_never_allowed_without_approval(
    p: Principal, tool: SimpleTool, ts: list[Target]
) -> None:
    d = _decide(p, tool, ts)
    if d.effective_risk == "critical":
        assert d.outcome != "allow"


@settings(max_examples=400)
@given(p=principals, tool=tools, ts=targets)
def test_tainted_non_read_never_allowed_without_approval(
    p: Principal, tool: SimpleTool, ts: list[Target]
) -> None:
    d = _decide(p, tool, ts)
    if p.tainted and TIER[d.effective_risk] > TIER["read"]:
        assert d.outcome != "allow"


@settings(max_examples=400)
@given(p=principals, tool=tools, ts=targets)
def test_ceiling_is_never_exceeded(p: Principal, tool: SimpleTool, ts: list[Target]) -> None:
    d = _decide(p, tool, ts)
    if d.outcome != "deny":
        assert TIER[d.effective_risk] <= TIER[PROFILES[p.profile_id].risk_ceiling]


@settings(max_examples=400)
@given(p=principals, tool=tools, ts=targets)
def test_new_entity_unreachable(p: Principal, tool: SimpleTool, ts: list[Target]) -> None:
    d = _decide(p, tool, ts)
    if any(ENTITIES.get(t.uuid, None) is None or ENTITIES[t.uuid].status != "active" for t in ts):
        # The only non-deny path through an unknown/new entity would be a direct ha_entity
        # grant on it, which no fixture grant has.
        assert d.outcome == "deny"


@settings(max_examples=400)
@given(p=principals, tool=tools, ts=targets)
def test_risk_only_raised(p: Principal, tool: SimpleTool, ts: list[Target]) -> None:
    d = _decide(p, tool, ts)
    assert TIER[d.effective_risk] >= TIER[tool.risk]
    r = effective_risk(tool, ts, SERVICES)
    if r != "blocked":
        assert d.effective_risk == r


@settings(max_examples=300)
@given(p=principals, tool=tools, ts=targets)
def test_deny_reason_vocabulary_and_determinism(
    p: Principal, tool: SimpleTool, ts: list[Target]
) -> None:
    d1 = _decide(p, tool, ts)
    d2 = _decide(p, tool, ts)
    assert d1 == d2
    if d1.outcome == "deny":
        assert d1.reason in DENY_REASONS
        assert d1.factors == ()
    elif d1.outcome == "confirm":
        assert d1.factors
        assert d1.reason is None
    else:
        assert d1.reason is None
        assert d1.factors == ()


@settings(max_examples=300)
@given(
    p=principals,
    tool=tools,
    ts=targets,
    mutate=st.sampled_from(["profile", "tool", "args", "targets", "exp", "executed"]),
)
def test_approval_binding(p: Principal, tool: SimpleTool, ts: list[Target], mutate: str) -> None:
    """A confirm can only become allow with an approval bound to the same profile, tool,
    args and targets, unexecuted and unexpired."""
    first = _decide(p, tool, ts)
    if first.outcome != "confirm":
        return
    good = Approval(
        id="A",
        profile_id=p.profile_id,
        tool=tool.name,
        args_digest=args_digest(tool.name, {"a": 1}, (t.uuid for t in ts)),
        exp=NOW + timedelta(seconds=10),
        decision="approved",
    )
    assert _decide(p, tool, ts, good).outcome == "allow"
    bad = _mutate(good, mutate)
    d = _decide(p, tool, ts, bad)
    assert d.outcome == "deny"
    assert d.reason == "approval_invalid"


def _mutate(a: Approval, how: str) -> Approval:
    """One field of a valid approval changed, by name."""
    if how == "profile":
        return Approval(a.id, "someone-else", a.tool, a.args_digest, a.exp, a.decision)
    if how == "tool":
        return Approval(a.id, a.profile_id, "other.tool", a.args_digest, a.exp, a.decision)
    if how == "args":
        return Approval(a.id, a.profile_id, a.tool, "deadbeef", a.exp, a.decision)
    if how == "targets":
        return Approval(
            a.id, a.profile_id, a.tool, args_digest(a.tool, {"a": 1}, ["x"]), a.exp, a.decision
        )
    if how == "exp":
        return Approval(a.id, a.profile_id, a.tool, a.args_digest, NOW, a.decision)
    return Approval(a.id, a.profile_id, a.tool, a.args_digest, a.exp, a.decision, executed_at=NOW)
