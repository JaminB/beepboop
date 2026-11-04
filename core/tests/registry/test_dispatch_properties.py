"""ROADMAP v0.1 acceptance 4, through the real ``Registry.dispatch`` with a spy handler:

* no handler runs unless ``policy.authorize`` said ``allow`` for that very call;
* a ``critical`` (or confirm-level) call never runs without an approval that the registry
  consumed: approved, unexpired, bound to the same approver, tool and request, single use;
* a tainted principal never runs a non-read tool without such an approval;
* a profile never executes above its ceiling; a ``new`` HA entity is unreachable.

Principals cover every ``via`` (voice, web, pat, oauth) and *test as* actors; grants and
profiles are generated, not fixed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from beepboop.module import Module, ToolContext, tool
from beepboop.module.types import ToolHandler
from beepboop.policy import (
    ALL_SCOPES,
    TIER,
    ApprovalService,
    EntityInfo,
    Grant,
    GrantIndex,
    MemoryApprovalStore,
    NeedsConfirmation,
    PolicyDenied,
    PolicyState,
    Principal,
    ProfileView,
    Risk,
    Target,
    approver_of,
    authorize,
)
from beepboop.policy.grants import ResourceType, SubjectType
from beepboop.policy.types import ProfileKind, Via
from beepboop.registry import ListAuditSink, RegisteredTool, Registry, parse_manifest

from .conftest import NOW, FakeContextFactory

TOOLS: dict[str, Risk] = {
    "demo.echo": "read",
    "demo.poke": "act",
    "demo.mine": "personal",
    "demo.unlock": "critical",
}
MANIFEST = parse_manifest(
    'id: demo\napi: 1\nversion: 0.1.0\nsummary: "Demo."\nscope: household\n'
    "category: system\ntools:\n"
    + "".join(
        f"  - name: {name}\n    description: {name}.\n    risk: {risk}\n"
        "    untrusted_output: false\n"
        "    args: {type: object, properties: {}, additionalProperties: false}\n"
        "    output: {type: object, properties: {speak: {type: string}}}\n"
        for name, risk in TOOLS.items()
    )
)
ENTITIES = {
    "e-active": EntityInfo("e-active", "active", area_id="kitchen"),
    "e-new": EntityInfo("e-new", "new", area_id="kitchen"),
}
PROFILE_IDS = ("alex", "sam", "mia", "guest")


class Spy(Module):
    def __init__(self) -> None:
        self.ran: list[tuple[str, Principal]] = []

    def _run(self, name: str, ctx: ToolContext) -> dict[str, Any]:
        assert ctx.principal is not None
        self.ran.append((name, ctx.principal))
        return {"speak": name}

    @tool("demo.echo")
    async def echo(self, ctx: ToolContext) -> dict[str, Any]:
        return self._run("demo.echo", ctx)

    @tool("demo.poke")
    async def poke(self, ctx: ToolContext) -> dict[str, Any]:
        return self._run("demo.poke", ctx)

    @tool("demo.mine")
    async def mine(self, ctx: ToolContext) -> dict[str, Any]:
        return self._run("demo.mine", ctx)

    @tool("demo.unlock")
    async def unlock(self, ctx: ToolContext) -> dict[str, Any]:
        return self._run("demo.unlock", ctx)


@dataclass
class Extractor:
    """A target extractor driven by the test: the same targets for every call."""

    targets: list[Target] = field(default_factory=list[Target])

    async def __call__(
        self, principal: Principal, tool: RegisteredTool, args: Mapping[str, Any]
    ) -> list[Target]:
        return list(self.targets)


# ----------------------------------------------------------------- strategies --

risks: st.SearchStrategy[Risk] = st.sampled_from(["read", "act", "personal", "critical"])
kinds: st.SearchStrategy[ProfileKind] = st.sampled_from(["admin", "member", "child", "guest"])


@st.composite
def profiles(draw: st.DrawFn) -> dict[str, ProfileView]:
    out: dict[str, ProfileView] = {}
    for pid in PROFILE_IDS:
        kind: ProfileKind = (
            "guest" if pid == "guest" else draw(kinds.filter(lambda k: k != "guest"))
        )
        ceiling: Risk = "act" if kind in ("child", "guest") else draw(risks)
        out[pid] = ProfileView(
            pid,
            kind,
            ceiling,
            frozenset({"household"}),
            has_login=draw(st.booleans()) if kind != "guest" else False,
            pin_enabled=draw(st.booleans()),
        )
    return out


@st.composite
def grants(draw: st.DrawFn) -> list[Grant]:
    out: list[Grant] = []
    n = draw(st.integers(0, 6))
    for i in range(n):
        subject_type: SubjectType = draw(st.sampled_from(["profile", "group"]))
        subject = "household" if subject_type == "group" else draw(st.sampled_from(PROFILE_IDS))
        resource_type: ResourceType = draw(st.sampled_from(["tool_glob", "ha_entity", "ha_area"]))
        if resource_type == "tool_glob":
            resource = draw(st.sampled_from(["*", "demo.*", *TOOLS]))
        elif resource_type == "ha_entity":
            resource = draw(st.sampled_from(["e-active", "e-new"]))
        else:
            resource = "kitchen"
        out.append(Grant(f"g{i}", subject_type, subject, resource_type, resource, draw(risks)))
    return out


@st.composite
def principals(draw: st.DrawFn) -> Principal:
    via: Via = draw(st.sampled_from(["voice", "web", "pat", "oauth"]))
    pid = draw(st.sampled_from(PROFILE_IDS))
    actor = None
    if via == "web" and pid != "alex" and draw(st.booleans()):
        actor = "alex"
    conf = draw(st.floats(0, 1)) if via == "voice" else None
    margin = draw(st.floats(0, 1)) if via == "voice" else None
    scopes = (
        ALL_SCOPES
        if via in ("voice", "web")
        else frozenset(draw(st.sets(st.sampled_from(sorted(ALL_SCOPES)))))
    )
    return Principal(pid, via, conf, margin, "dev", scopes, draw(st.booleans()), "T1", actor)


targets = st.lists(
    st.builds(
        Target,
        uuid=st.sampled_from(["e-active", "e-new"]),
        domain=st.sampled_from(["light", "lock"]),
        service=st.sampled_from(["turn_on", "unlock", None]),
    ),
    max_size=2,
)


def _registry(state: PolicyState, extractor: Extractor) -> tuple[Registry, Spy]:
    counter = iter(range(10_000))
    spy = Spy()
    reg = Registry(
        policy_state=lambda: state,
        approvals=ApprovalService(MemoryApprovalStore(), lambda: f"A{next(counter)}"),
        context_factory=FakeContextFactory(),
        audit=ListAuditSink(),
        targets=extractor,
        clock=lambda: NOW,
    )
    reg.register_module(MANIFEST, spy, "# demo\n")
    return reg, spy


async def _try(reg: Registry, p: Principal, name: str, approval: Any = None) -> tuple[str, Any]:
    try:
        out = await reg.dispatch(p, name, {}, approval=approval)
    except PolicyDenied as exc:
        return "deny", exc
    except NeedsConfirmation as exc:
        return "confirm", exc
    return "allow", out


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    profs=profiles(), gs=grants(), p=principals(), ts=targets, name=st.sampled_from(sorted(TOOLS))
)
def test_handler_runs_only_when_authorize_allows(
    profs: dict[str, ProfileView], gs: list[Grant], p: Principal, ts: list[Target], name: str
) -> None:
    state = PolicyState(profiles=profs, grants=GrantIndex.build(gs), entities=ENTITIES)
    reg, spy = _registry(state, Extractor(ts))
    kind, _ = asyncio.run(_try(reg, p, name))
    expected = authorize(state, p, reg.get(name), {}, ts, NOW)
    assert kind == expected.outcome
    assert bool(spy.ran) == (expected.outcome == "allow")
    if spy.ran:
        assert spy.ran == [(name, p)]
        assert TIER[expected.effective_risk] <= TIER[profs[p.profile_id].risk_ceiling]
        assert not any(ENTITIES[t.uuid].status == "new" for t in ts)
        assert expected.effective_risk != "critical"
        assert not (p.tainted and expected.effective_risk != "read")


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    profs=profiles(),
    gs=grants(),
    p=principals(),
    ts=targets,
    name=st.sampled_from(sorted(TOOLS)),
    mutate=st.sampled_from(["none", "deny", "other_profile", "other_tool", "expired", "reuse"]),
)
def test_confirm_executes_only_with_a_consumed_bound_approval(
    profs: dict[str, ProfileView],
    gs: list[Grant],
    p: Principal,
    ts: list[Target],
    name: str,
    mutate: str,
) -> None:
    state = PolicyState(profiles=profs, grants=GrantIndex.build(gs), entities=ENTITIES)
    reg, spy = _registry(state, Extractor(ts))

    async def run() -> None:
        kind, exc = await _try(reg, p, name)
        if kind != "confirm":
            return
        record, nonce = exc.approval, exc.nonce
        assert record.profile_id == approver_of(p)
        assert spy.ran == []
        if mutate == "deny":
            await reg.approvals.decide(record.id, approver_of(p), "denied", "web", NOW, nonce)
            decided = await reg.approvals.store.get(record.id)
            k2, _ = await _try(reg, p, name, decided)
            assert k2 == "deny"
            assert spy.ran == []
            return
        if mutate == "other_profile":
            assert (
                await reg.approvals.decide(record.id, "nobody", "approved", "web", NOW, nonce)
                == "not_yours"
            )
            return
        decided = await reg.approvals.decide(
            record.id, approver_of(p), "approved", "web", NOW, nonce
        )
        assert not isinstance(decided, str)
        if mutate == "other_tool":
            other = next(t for t in TOOLS if t != name)
            k2, _ = await _try(reg, p, other, decided)
            assert spy.ran == []
            assert k2 != "allow"
            return
        if mutate == "expired":
            reg.clock = lambda: NOW + timedelta(seconds=61)
            k2, _ = await _try(reg, p, name, decided)
            assert k2 == "deny"
            assert spy.ran == []
            return
        k2, _ = await _try(reg, p, name, decided)
        assert k2 == "allow"
        assert spy.ran == [(name, p)]
        consumed = await reg.approvals.store.get(record.id)
        assert consumed is not None
        assert consumed.executed_at is not None
        if mutate == "reuse":
            k3, _ = await _try(reg, p, name, decided)
            assert k3 == "deny"
            assert len(spy.ran) == 1

    asyncio.run(run())


def test_handler_type_is_the_sdk_handler() -> None:
    handler: ToolHandler = Spy().tools()["demo.echo"]
    assert callable(handler)
