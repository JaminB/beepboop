"""Shared fixtures for the LLM provider tests (recorded Bedrock streams)."""

from __future__ import annotations

from pathlib import Path

import pytest

from beepboop.providers.llm.bedrock.fake import RawEvent, load_fixture

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def text_events() -> list[RawEvent]:
    """A real Haiku 5.5 two-sentence answer (conformance.text_request())."""
    return load_fixture(FIXTURES / "bedrock_text_streams.json")


@pytest.fixture
def tool_events() -> list[RawEvent]:
    """A real Haiku 5.5 home.turn_off call (conformance.tool_request())."""
    return load_fixture(FIXTURES / "bedrock_tool_call.json")
