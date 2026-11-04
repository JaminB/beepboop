"""The approval state machine (§8.2) in memory, and the PIN lockout rules (§8.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from beepboop.policy import (
    APPROVAL_TTL,
    ApprovalService,
    MemoryApprovalStore,
    PinState,
    TooManyPendingError,
    hash_pin,
    record_outcome,
    verify_pin,
)

from .conftest import NOW, voice


@pytest.fixture
def service() -> ApprovalService:
    counter = iter(range(1000))
    return ApprovalService(MemoryApprovalStore(), lambda: f"A{next(counter)}")


async def test_create_binds_profile_digest_and_ttl(service: ApprovalService) -> None:
    rec, nonce = await service.create(voice("alex"), "home.set", {"x": 1}, ["lock"], "Unlock", NOW)
    assert rec.profile_id == "alex"
    assert rec.exp == NOW + APPROVAL_TTL
    assert rec.pending
    assert len(nonce) == 32
    assert rec.turn_id == "01TURN"


async def test_test_as_approval_goes_to_actor(service: ApprovalService) -> None:
    p = replace(voice("mia"), actor_profile_id="alex")
    rec, _ = await service.create(p, "home.set", {}, [], "x", NOW)
    assert rec.profile_id == "alex"


async def test_decide_once_same_profile_right_nonce(service: ApprovalService) -> None:
    rec, nonce = await service.create(voice("alex"), "home.set", {}, [], "x", NOW)
    assert await service.decide(rec.id, "sam", "approved", "web", NOW, nonce) == "not_yours"
    assert await service.decide(rec.id, "alex", "approved", "web", NOW, "bad") == "bad_nonce"
    assert await service.decide(rec.id, "alex", "approved", "web", NOW) == "bad_nonce"
    done = await service.decide(rec.id, "alex", "approved", "web", NOW, nonce)
    assert not isinstance(done, str)
    assert done.decision == "approved"
    assert await service.decide(rec.id, "alex", "approved", "web", NOW, nonce) == "already_decided"
    assert await service.decide("nope", "alex", "approved", "web", NOW, nonce) == "not_found"


async def test_pin_factor_needs_no_nonce(service: ApprovalService) -> None:
    rec, _ = await service.create(voice("alex"), "home.set", {}, [], "x", NOW)
    done = await service.decide(rec.id, "alex", "approved", "pin", NOW)
    assert not isinstance(done, str)
    assert done.factor == "pin"


async def test_expired_cannot_be_decided(service: ApprovalService) -> None:
    rec, nonce = await service.create(voice("alex"), "home.set", {}, [], "x", NOW)
    late = NOW + timedelta(seconds=61)
    assert await service.decide(rec.id, "alex", "approved", "web", late, nonce) == "expired"


async def test_execute_once(service: ApprovalService) -> None:
    rec, nonce = await service.create(voice("alex"), "home.set", {}, [], "x", NOW)
    assert await service.consume(rec.id, NOW) is None  # not approved yet
    await service.decide(rec.id, "alex", "approved", "web", NOW, nonce)
    first = await service.consume(rec.id, NOW)
    assert first is not None
    assert first.executed_at == NOW
    assert await service.consume(rec.id, NOW) is None


async def test_extend_once(service: ApprovalService) -> None:
    rec, _ = await service.create(voice("alex"), "home.set", {}, [], "x", NOW)
    assert await service.extend_once(rec.id, NOW)
    got = await service.store.get(rec.id)
    assert got is not None
    assert got.exp == NOW + timedelta(seconds=120)
    assert not await service.extend_once(rec.id, NOW)


async def test_max_three_pending(service: ApprovalService) -> None:
    for _ in range(3):
        await service.create(voice("alex"), "home.set", {}, [], "x", NOW)
    with pytest.raises(TooManyPendingError):
        await service.create(voice("alex"), "home.set", {}, [], "x", NOW)


# ------------------------------------------------------------------- PIN --


def test_hash_and_verify_pin() -> None:
    h = hash_pin("4719")
    assert h.startswith("$argon2id$")
    assert verify_pin(h, "4719")
    assert not verify_pin(h, "0000")
    with pytest.raises(ValueError, match="digits"):
        hash_pin("12")


def test_three_failures_lock_for_five_minutes() -> None:
    s = PinState(enabled=True)
    for i in range(2):
        t = record_outcome(s, "mismatch", NOW + timedelta(minutes=i))
        assert t.event == "mismatch"
        s = t.state
    t = record_outcome(s, "mismatch", NOW + timedelta(minutes=2))
    assert t.event == "locked"
    assert t.state.is_locked(NOW + timedelta(minutes=3))
    assert not t.state.is_locked(NOW + timedelta(minutes=8))


def test_failures_outside_window_do_not_accumulate() -> None:
    s = record_outcome(PinState(enabled=True), "mismatch", NOW).state
    s = record_outcome(s, "mismatch", NOW + timedelta(minutes=16)).state
    assert s.failures == 1


def test_ten_failures_in_a_day_disable() -> None:
    s = PinState(enabled=True)
    event = ""
    for i in range(10):
        t = record_outcome(s, "mismatch", NOW + timedelta(minutes=20 * i))
        s, event = t.state, t.event
    assert event == "disabled"
    assert not s.enabled


def test_success_resets_and_timeouts_ignored() -> None:
    s = record_outcome(PinState(enabled=True, failures=2), "ok", NOW)
    assert s.state.failures == 0
    assert s.event == "ok"
    assert record_outcome(s.state, "timeout", NOW).event == "ignored"
