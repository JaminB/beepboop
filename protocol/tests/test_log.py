"""Tests for structlog configuration and secret redaction (hard rule 5)."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pytest
import structlog
from hypothesis import given
from hypothesis import strategies as st

from beepboop_protocol.log import (
    REDACTED,
    configure_logging,
    get_logger,
    is_sensitive_key,
    redact_secrets,
)


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Start and end every test from structlog's defaults so no config leaks to other suites."""
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


@pytest.mark.parametrize(
    "key", ["secret", "client_secret", "ha_token", "Password", "pin", "spoken_PIN", "pin_hash"]
)
def test_sensitive_keys(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ["tool", "turn_id", "profile", "event", "speaker"])
def test_ordinary_keys(key: str) -> None:
    assert not is_sensitive_key(key)


def test_redacts_top_level_and_nested() -> None:
    event = {
        "event": "ha.connect",
        "token": "abc",
        "config": {"url": "http://ha", "access_token": "xyz", "nested": [{"pin": "1234"}]},
    }
    out = redact_secrets(None, "info", event)
    assert out["token"] == REDACTED
    assert out["config"]["access_token"] == REDACTED
    assert out["config"]["url"] == "http://ha"
    assert out["config"]["nested"][0]["pin"] == REDACTED
    assert event["token"] == "abc", "input must not be mutated"


@given(st.text(min_size=1), st.text())
def test_any_key_containing_secret_is_redacted(prefix: str, value: str) -> None:
    out = redact_secrets(None, "info", {f"{prefix}secret": value, "event": "x"})
    assert out[f"{prefix}secret"] == REDACTED


def test_configured_logger_emits_redacted_json_with_turn_id() -> None:
    stream = io.StringIO()
    configure_logging("debug", stream=stream)
    structlog.contextvars.bind_contextvars(turn_id="t-1")
    get_logger("test").info("tool.called", tool="home.turn_on", api_token="s3cr3t")

    line = json.loads(stream.getvalue())
    assert line["event"] == "tool.called"
    assert line["turn_id"] == "t-1"
    assert line["logger"] == "test"
    assert line["level"] == "info"
    assert line["api_token"] == REDACTED
    assert "s3cr3t" not in stream.getvalue()


def test_level_filtering() -> None:
    stream = io.StringIO()
    configure_logging("warning", stream=stream)
    get_logger().info("quiet")
    assert stream.getvalue() == ""


def test_module_level_logger_picks_up_later_configuration() -> None:
    """A logger created before configure_logging() must still redact (hard rule 5)."""
    early = get_logger("early")
    stream = io.StringIO()
    configure_logging("debug", stream=stream)
    early.info("ha.token_minted", ha_token="s3cr3t")
    line = json.loads(stream.getvalue())
    assert line["ha_token"] == REDACTED
    assert line["logger"] == "early"
