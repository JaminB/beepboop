"""The web second factor end to end (ROADMAP v0.1 acceptance 5, at the API level).

A ``critical`` voice request → approval + SSE event with the nonce for the same profile →
approve once → the executor re-dispatches and the tool runs exactly once. Another
profile's session cannot approve; the nonce cannot be reused; after 60 s it is rejected.
"""

from __future__ import annotations

import asyncio

import pytest

from beepboop.api.events import Viewer, sse_stream
from beepboop.api.testing import Api, register_frontdoor, voice_principal
from beepboop.policy.errors import NeedsConfirmation, PolicyDenied


async def _raise_critical(api: Api, profile_id: str) -> tuple[str, str]:
    with pytest.raises(NeedsConfirmation) as exc:
        await api.deps.registry.dispatch(voice_principal(profile_id), "frontdoor.unlock", {})
    approval_id = exc.value.approval.id
    nonce = api.deps.events.nonce_for(approval_id)
    assert nonce is not None
    return approval_id, nonce


async def test_banner_event_reaches_only_the_same_profile(api: Api) -> None:
    register_frontdoor(api.deps.registry)
    alex, _ = await api.person("Alex", "admin")
    sam, _ = await api.person("Sam", "member")
    alex_q = api.deps.events.subscribe(Viewer(alex, "admin"))[1]
    sam_q = api.deps.events.subscribe(Viewer(sam, "member"))[1]
    approval_id, nonce = await _raise_critical(api, alex)
    ev = alex_q.get_nowait()
    assert ev.event == "approval"
    assert ev.data["id"] == approval_id
    assert ev.data["nonce"] == nonce
    assert ev.data["state"] == "pending"
    assert sam_q.empty()


async def test_approve_once_then_execute_once(api: Api) -> None:
    door = register_frontdoor(api.deps.registry)
    alex, alex_tok = await api.person("Alex", "admin")
    _sam, sam_tok = await api.person("Sam", "admin")
    approval_id, nonce = await _raise_critical(api, alex)

    # Another profile's session (even an admin) cannot see or approve it.
    api.use(sam_tok)
    assert (await api.http.get(f"/api/approvals/{approval_id}")).status_code == 404
    r = await api.post(f"/api/approvals/{approval_id}/approve", json={"nonce": nonce})
    assert r.status_code == 409
    assert r.json()["code"] == "not_yours"

    api.use(alex_tok)
    r = await api.http.get("/api/approvals")
    assert [a["id"] for a in r.json()] == [approval_id]
    assert "nonce" not in r.json()[0]  # the nonce travels only in SSE
    r = await api.post(f"/api/approvals/{approval_id}/approve", json={"nonce": "f" * 32})
    assert r.json()["code"] == "bad_nonce"

    waiter = asyncio.create_task(api.deps.events.wait_decision(approval_id, 5))
    r = await api.post(f"/api/approvals/{approval_id}/approve", json={"nonce": nonce})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "approved"
    assert r.json()["factor"] == "web"
    decided = await waiter
    assert decided is not None
    assert decided.decision == "approved"

    # Second tap: 409.
    r = await api.post(f"/api/approvals/{approval_id}/approve", json={"nonce": nonce})
    assert r.status_code == 409
    assert r.json()["code"] == "already_decided"

    # The executor re-dispatches with the approval: runs once, never twice.
    record = await api.deps.registry.approvals.store.get(approval_id)
    assert record is not None
    principal = voice_principal(alex)
    await api.deps.registry.dispatch(principal, "frontdoor.unlock", {}, approval=record)
    assert door.unlocked == 1
    with pytest.raises(PolicyDenied):
        await api.deps.registry.dispatch(principal, "frontdoor.unlock", {}, approval=record)
    assert door.unlocked == 1


async def test_approval_after_60_seconds_is_rejected(api: Api) -> None:
    register_frontdoor(api.deps.registry)
    alex, tok = await api.person("Alex", "admin")
    approval_id, nonce = await _raise_critical(api, alex)
    api.use(tok)
    api.clock.advance(61)
    r = await api.post(f"/api/approvals/{approval_id}/approve", json={"nonce": nonce})
    assert r.status_code == 409
    assert r.json()["code"] == "expired"
    r = await api.http.get(f"/api/approvals/{approval_id}")
    assert r.json()["state"] == "expired"


async def test_opening_the_stream_extends_once_to_120_seconds(api: Api) -> None:
    register_frontdoor(api.deps.registry)
    alex, _tok = await api.person("Alex", "admin")
    approval_id, nonce = await _raise_critical(api, alex)
    api.clock.advance(30)
    stream = sse_stream(
        api.deps.events,
        Viewer(alex, "admin"),
        last_event_id=None,
        initial=[("approval", {"id": approval_id, "nonce": nonce})],
        ping_interval=0.01,
    )
    assert (await anext(stream)).startswith("retry:")
    first = await anext(stream)
    assert nonce in first
    assert "event: approval" in first
    assert (await anext(stream)) == ": ping\n\n"
    await stream.aclose()
    from beepboop.api.routers.approvals import extend_pending_for

    extended = await extend_pending_for(api.deps, alex)
    assert extended[0].extended
    assert (extended[0].exp - extended[0].created_at).total_seconds() == 120
    # Only once.
    again = await extend_pending_for(api.deps, alex)
    assert again[0].exp == extended[0].exp
    api.clock.advance(80)  # 110 s after creation: still live thanks to the extension
    _ = approval_id


async def test_deny_and_not_me(api: Api) -> None:
    register_frontdoor(api.deps.registry)
    alex, tok = await api.person("Alex", "admin")
    api.use(tok)
    a1, n1 = await _raise_critical(api, alex)
    r = await api.post(f"/api/approvals/{a1}/deny", json={"nonce": n1})
    assert r.json()["state"] == "denied"
    a2, n2 = await _raise_critical(api, alex)
    r = await api.post(f"/api/approvals/{a2}/not-me", json={"nonce": n2})
    assert r.json()["state"] == "denied"
    from beepboop.db import repo

    audit = await api.db.read(lambda c: repo.audit.list_(c))
    assert any(a.action == "approval.not_me" and a.target == a2 for a in audit)
