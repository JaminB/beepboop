"""The protocol models (docs/LLM_PROVIDERS.md §2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from beepboop.providers.llm import (
    Done,
    Message,
    ProviderError,
    Request,
    RequestMetadata,
    SystemBlock,
    TextPart,
    Throttled,
    ToolResultPart,
    ToolUsePart,
)


def test_message_parts_are_discriminated_by_type() -> None:
    message = Message.model_validate(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "t1", "name": "home.turn_on", "input": {}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ],
        }
    )
    assert [type(p) for p in message.content] == [TextPart, ToolUsePart, ToolResultPart]


def test_request_defaults_match_the_spec() -> None:
    req = Request(
        system=[SystemBlock(text="s")],
        messages=[],
        metadata=RequestMetadata(profile_id="p", turn_id="t"),
    )
    assert (req.max_tokens, req.effort, req.tools, req.stop) == (400, "low", [], [])
    assert req.system[0].ttl == "5m"
    assert req.metadata.kind == "turn"


def test_models_are_frozen_and_strict() -> None:
    done = Done(stop_reason="end_turn")
    with pytest.raises(ValidationError):
        done.stop_reason = "error"
    with pytest.raises(ValidationError):
        Done.model_validate({"stop_reason": "end_turn", "surprise": 1})
    with pytest.raises(ValidationError):
        Done.model_validate({"stop_reason": "finished"})


def test_errors_carry_the_vendor_code() -> None:
    error = Throttled("slow down", code="ThrottlingException")
    assert isinstance(error, ProviderError)
    assert error.code == "ThrottlingException"
    assert str(error) == "slow down"
