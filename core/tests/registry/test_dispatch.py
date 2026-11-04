"""``registry.dispatch``: authorize → execute → audit for every outcome (ARCHITECTURE §6)."""

from __future__ import annotations

import pytest

from beepboop.policy import ALL_SCOPES, NeedsConfirmation, PolicyDenied, Principal
from beepboop.registry import ArgsInvalidError, ListAuditSink, Registry, ToolNotFoundError

from .conftest import NOW, voice


async def test_allow_runs_handler_and_audits(registry: Registry, audit: ListAuditSink) -> None:
    out = await registry.dispatch(voice("alex"), "demo.echo", {"text": "hi"})
    assert out.result.structured["speak"] == "you said hi"
    assert out.decision.outcome == "allow"
    assert not out.tainted
    assert [a.result_status for a in audit.entries] == ["ok"]
    assert audit.entries[0].args_redacted == {"text": "hi"}


async def test_deny_raises_and_audits_without_running(
    registry: Registry, audit: ListAuditSink
) -> None:
    with pytest.raises(PolicyDenied) as exc:
        await registry.dispatch(voice("guest"), "demo.unlock", {})
    assert exc.value.reason == "ceiling"
    assert audit.entries[-1].decision.outcome == "deny"
    assert audit.entries[-1].result_status == "denied"


async def test_critical_confirms_creates_approval(registry: Registry, audit: ListAuditSink) -> None:
    with pytest.raises(NeedsConfirmation) as exc:
        await registry.dispatch(voice("alex"), "demo.unlock", {})
    approval = exc.value.approval
    assert approval.profile_id == "alex"
    assert approval.tool == "demo.unlock"
    assert exc.value.factors == ("web",)
    assert audit.entries[-1].decision.outcome == "confirm"
    assert audit.entries[-1].approval_id == approval.id


async def test_redispatch_with_approved_record_consumes_once(registry: Registry) -> None:
    with pytest.raises(NeedsConfirmation) as exc:
        await registry.dispatch(voice("alex"), "demo.unlock", {})
    rec, nonce = exc.value.approval, exc.value.nonce
    decided = await registry.approvals.decide(rec.id, "alex", "approved", "web", NOW, nonce)
    assert not isinstance(decided, str)
    out = await registry.dispatch(voice("alex"), "demo.unlock", {}, approval=decided)
    assert out.result.structured["approval"] == rec.id
    # second use: executed_at is set, authorize says approval_invalid
    with pytest.raises(PolicyDenied) as denied:
        await registry.dispatch(voice("alex"), "demo.unlock", {}, approval=decided)
    assert denied.value.reason == "approval_invalid"


async def test_approval_for_other_args_is_invalid(registry: Registry) -> None:
    with pytest.raises(NeedsConfirmation) as exc:
        await registry.dispatch(voice("alex"), "demo.unlock", {})
    rec = exc.value.approval
    await registry.approvals.decide(rec.id, "alex", "approved", "web", NOW, exc.value.nonce)
    approved = await registry.approvals.store.get(rec.id)
    assert approved is not None
    with pytest.raises(PolicyDenied) as denied:
        await registry.dispatch(voice("alex"), "demo.echo", {"text": "x"}, approval=approved)
    assert denied.value.reason == "approval_invalid"


async def test_failed_approval_use_is_audited(registry: Registry, audit: ListAuditSink) -> None:
    """IDENTITY_AND_TRUST §11: every approval execution is written, a rejected one too."""
    with pytest.raises(NeedsConfirmation) as exc:
        await registry.dispatch(voice("alex"), "demo.unlock", {})
    rec = exc.value.approval
    await registry.approvals.decide(rec.id, "alex", "approved", "web", NOW, exc.value.nonce)
    approved = await registry.approvals.store.get(rec.id)
    assert approved is not None
    before = len(audit.entries)
    with pytest.raises(PolicyDenied):
        await registry.dispatch(voice("alex"), "demo.poke", {}, approval=approved)
    last = audit.entries[-1]
    assert len(audit.entries) == before + 1
    assert last.decision.reason == "approval_invalid"
    assert last.approval_id == rec.id
    assert last.result_status == "denied"


async def test_test_as_approval_goes_to_and_comes_from_the_actor(registry: Registry) -> None:
    """IDENTITY_AND_TRUST §3.1: a *test as* approval is created for the acting admin and
    only the actor's approval executes it; the child's own profile cannot satisfy it."""
    # Mia has no login; the tainted act call still gets the web factor because Alex (the
    # actor and approver) has one.
    as_mia = Principal("mia", "web", None, None, "web:x", ALL_SCOPES, True, "T2", "alex")
    args: dict[str, object] = {}
    with pytest.raises(NeedsConfirmation) as exc:
        await registry.dispatch(as_mia, "demo.poke", args)
    rec = exc.value.approval
    assert rec.profile_id == "alex"
    assert exc.value.factors == ("web",)
    nonce = exc.value.nonce
    assert await registry.approvals.decide(rec.id, "mia", "approved", "web", NOW, nonce) == (
        "not_yours"
    )
    decided = await registry.approvals.decide(rec.id, "alex", "approved", "web", NOW, nonce)
    assert not isinstance(decided, str)
    out = await registry.dispatch(as_mia, "demo.poke", args, approval=decided, dry_run=True)
    assert out.decision.outcome == "allow"


async def test_untrusted_output_taints(registry: Registry) -> None:
    out = await registry.dispatch(voice("alex"), "demo.feed", {})
    assert out.tainted
    with pytest.raises(NeedsConfirmation):
        await registry.dispatch(voice("alex", tainted=True), "demo.poke", {})
    # read stays allowed while tainted (IDENTITY_AND_TRUST §4)
    allowed = await registry.dispatch(voice("alex", tainted=True), "demo.echo", {"text": "x"})
    assert allowed.decision.outcome == "allow"


async def test_tool_error_is_reported_not_raised(registry: Registry, audit: ListAuditSink) -> None:
    out = await registry.dispatch(voice("alex"), "demo.fail", {})
    assert out.result.structured["code"] == "unavailable"
    assert audit.entries[-1].result_status == "error"
    assert audit.entries[-1].error == "unavailable"


async def test_bad_args_and_unknown_tool(registry: Registry) -> None:
    with pytest.raises(ArgsInvalidError):
        await registry.dispatch(voice("alex"), "demo.echo", {"nope": 1})
    with pytest.raises(ToolNotFoundError):
        await registry.dispatch(voice("alex"), "demo.missing", {})


def test_callable_by_and_always_loaded(registry: Registry) -> None:
    names = {t.name for t in registry.callable_by(voice("mia"))}
    assert names == {"demo.echo", "demo.poke"}
    assert {t.name for t in registry.callable_by(voice("alex"))} >= {"demo.echo", "demo.unlock"}
    assert registry.always_loaded() == []  # demo tools are deferred; no builtins registered


def test_redact_args() -> None:
    from beepboop.registry import parse_manifest, redact_args

    m = parse_manifest(
        """
id: sx
api: 1
version: 0.1.0
summary: "S."
scope: household
category: system
tools:
  - name: sx.x
    description: X.
    risk: read
    untrusted_output: false
    args:
      type: object
      properties:
        pin: {type: string, description: "p", x-sensitive: true}
        note: {type: string, description: "n"}
      additionalProperties: false
    output: {type: object, properties: {speak: {type: string}}}
"""
    )
    spec = m.tools[0]
    assert spec.sensitive_args == {"pin"}
    out = redact_args(spec, {"pin": "1234", "note": "x" * 600})
    assert out["pin"] == "[redacted]"
    assert len(out["note"]) == 501
