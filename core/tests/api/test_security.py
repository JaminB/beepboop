"""Credentials, claim codes, throttling and the SSE filter: pure units (WEB_UI §6, §8)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beepboop.api.claim import CLAIM_ATTEMPTS, CLAIM_TTL, ClaimManager
from beepboop.api.events import BusEvent, EventBus, Viewer, format_sse, visible
from beepboop.api.ratelimit import IP_FAILURES, TOTP_FAILURES, USER_FAILURES, LoginThrottle
from beepboop.api.security import (
    PasswordPolicyError,
    Passwords,
    check_password_policy,
    consume_recovery_code,
    hash_recovery_code,
    new_recovery_codes,
    new_totp_secret,
    provisioning_uri,
    totp_code,
    verify_totp,
)

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


def test_totp_matches_rfc6238_sha1_vectors() -> None:
    # RFC 6238 Appendix B (SHA-1), truncated to our 6 digits.
    assert totp_code(RFC_SECRET, 59 // 30) == "287082"
    assert totp_code(RFC_SECRET, 1111111109 // 30) == "081804"
    assert totp_code(RFC_SECRET, 1234567890 // 30) == "005924"


def test_totp_drift_and_replay() -> None:
    secret = new_totp_secret()
    step = int(T0.timestamp()) // 30
    assert verify_totp(secret, totp_code(secret, step), T0, None) == step
    assert verify_totp(secret, totp_code(secret, step - 1), T0, None) == step - 1
    assert verify_totp(secret, totp_code(secret, step + 1), T0, None) == step + 1
    assert verify_totp(secret, totp_code(secret, step - 2), T0, None) is None
    # The last-used step blocks replay of the same (or an older) code.
    assert verify_totp(secret, totp_code(secret, step), T0, step) is None
    assert verify_totp(secret, "12345", T0, None) is None
    assert verify_totp(secret, "abcdef", T0, None) is None


def test_provisioning_uri_is_otpauth() -> None:
    uri = provisioning_uri("ABC", "alex")
    assert uri.startswith("otpauth://totp/beepboop%3Aalex?secret=ABC")
    assert "digits=6" in uri
    assert "period=30" in uri


def test_password_policy() -> None:
    with pytest.raises(PasswordPolicyError) as e:
        check_password_policy("short")
    assert e.value.code == "password_too_short"
    with pytest.raises(PasswordPolicyError) as e:
        check_password_policy("password1234")
    assert e.value.code == "password_too_common"
    with pytest.raises(PasswordPolicyError):
        check_password_policy("alexalexalex", username="alexalexalex")
    check_password_policy("correct horse battery")


def test_passwords_hash_and_verify() -> None:
    pw = Passwords(memory_kib=8, time_cost=1, parallelism=1)
    h = pw.hash("correct horse battery")
    assert h.startswith("$argon2id$")
    assert pw.verify(h, "correct horse battery")
    assert not pw.verify(h, "wrong horse battery")
    assert not pw.verify("not-a-hash", "x")
    pw.dummy_verify("anything")


def test_default_password_parameters_are_the_spec() -> None:
    pw = Passwords()
    assert (pw.memory_kib, pw.time_cost, pw.parallelism) == (64 * 1024, 3, 1)


@given(st.integers(min_value=0, max_value=9))
def test_recovery_codes_are_single_use(i: int) -> None:
    codes = new_recovery_codes()
    assert len(codes) == 10
    assert all(len(c) == 8 for c in codes)
    hashes = [hash_recovery_code(c) for c in codes]
    remaining = consume_recovery_code(hashes, codes[i].upper())
    assert remaining is not None
    assert len(remaining) == 9
    assert consume_recovery_code(remaining, codes[i]) is None


def test_claim_code_single_use_ttl_and_attempts() -> None:
    spoken: list[str] = []
    m = ClaimManager(on_code=lambda code, _mode: spoken.append(code))
    assert m.check("000000", T0) == ("none", None)
    code = m.issue(T0)
    assert spoken == [code]
    assert len(code) == 6
    assert code.isdigit()
    assert m.mode(T0) == "first_admin"
    assert m.check(code, T0) == ("ok", "first_admin")
    assert m.check(code, T0)[0] == "none"  # single use

    code = m.issue(T0)
    wrong = "111111" if code != "111111" else "222222"
    for _ in range(CLAIM_ATTEMPTS - 1):
        assert m.check(wrong, T0)[0] == "wrong"
    assert m.check(wrong, T0)[0] == "exhausted"
    assert m.check(code, T0)[0] == "exhausted"

    code = m.issue(T0, "recovery")
    assert m.check(code, T0 + CLAIM_TTL)[0] == "expired"
    assert m.mode(T0 + CLAIM_TTL) is None


def test_login_throttle_user_ip_and_totp() -> None:
    t = LoginThrottle()
    for i in range(USER_FAILURES - 1):
        assert not t.password_failed("alex", f"10.0.0.{i}", T0)
    assert t.retry_after("alex", "10.0.0.99", T0) == 0
    assert t.password_failed("alex", "10.0.0.50", T0)
    assert t.retry_after("ALEX", "10.0.0.99", T0) > 0  # case-insensitive usernames
    assert t.retry_after("alex", "10.0.0.99", T0 + timedelta(minutes=2)) == 0
    t.succeeded("alex")
    assert t.retry_after("alex", "10.0.0.99", T0) == 0

    t = LoginThrottle()
    for i in range(IP_FAILURES):
        t.password_failed(f"user{i}", "10.9.9.9", T0)
    assert t.retry_after("someone", "10.9.9.9", T0) > 0
    assert t.retry_after("someone", "10.9.9.8", T0) == 0

    t = LoginThrottle()
    for _ in range(TOTP_FAILURES - 1):
        assert not t.totp_failed("sam", T0)
    assert t.totp_failed("sam", T0)
    assert 0 < t.retry_after("sam", "1.1.1.1", T0) <= 300


ADMIN = Viewer("a", "admin")
MEMBER = Viewer("m", "member")
OTHER = Viewer("o", "member")


def _ev(audience: str, subject: str | None, kind: str | None = None) -> BusEvent:
    return BusEvent(
        1,
        "turn",
        {"id": "t", "transcript": "unlock the door", "reply_delta": "ok", "state": "done"},
        audience,  # type: ignore[arg-type]
        subject,
        kind,  # type: ignore[arg-type]
    )


def test_visibility_table() -> None:
    # Approvals go to their profile only; admins cannot see others' approvals.
    approval = _ev("subject", "m")
    assert visible(approval, MEMBER) is not None
    assert visible(approval, ADMIN) is None
    assert visible(approval, OTHER) is None
    # Turn events: own full; admin sees children in full, other adults without transcripts.
    adult_turn = _ev("turn", "m", "member")
    assert visible(adult_turn, MEMBER) == adult_turn.data
    assert visible(adult_turn, OTHER) is None
    redacted = visible(adult_turn, ADMIN)
    assert redacted is not None
    assert "transcript" not in redacted
    assert "reply_delta" not in redacted
    child_turn = _ev("turn", "c", "child")
    assert visible(child_turn, ADMIN) == child_turn.data
    assert visible(_ev("admins", None), MEMBER) is None
    assert visible(_ev("admins", None), ADMIN) is not None
    assert visible(_ev("all", None), OTHER) is not None
    assert visible(_ev("subject_and_admins", "m"), ADMIN) is not None
    assert visible(_ev("subject_and_admins", "m"), OTHER) is None


def test_sse_format_and_replay_buffer() -> None:
    assert format_sse(3, "inbox", {"count": 2}) == 'id: 3\nevent: inbox\ndata: {"count":2}\n\n'
    bus = EventBus()
    bus.publish("inbox", {"count": 1}, audience="admins")
    bus.publish("device", {"state": "idle"}, audience="all")
    assert [e.event for e in bus.replay_after(0, MEMBER)] == ["device"]
    assert [e.event for e in bus.replay_after(0, ADMIN)] == ["inbox", "device"]
    assert bus.replay_after(2, ADMIN) == []
