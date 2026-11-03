"""The LLM provider conformance suite (docs/LLM_PROVIDERS.md §6), as reusable checks.

Each ``check_*`` coroutine drives any ``LLMProvider`` and raises
``ConformanceError`` with a plain explanation when the provider breaks the
contract. They are framework-free so a pytest file (with recorded fixtures,
no network) and ``beepboop bench llm`` (live) can share them.

M0 requires ``check_text_streams`` and ``check_cancel_mid_stream`` (ROADMAP
M0 acceptance 5); the rest of §6 is implemented where a recorded fixture is
enough, and the model-behaviour checks are v0.1 work.
"""

from beepboop.providers.llm.conformance.checks import (
    CANCEL_CLOSE_BUDGET_MS,
    ConformanceError,
    assert_stream_shape,
    check_cancel_mid_stream,
    check_no_secrets_on_wire,
    check_text_streams,
    check_tool_call_streams_args,
    check_usage_reported,
    collect,
    text_request,
    tool_request,
)

__all__ = [
    "CANCEL_CLOSE_BUDGET_MS",
    "ConformanceError",
    "assert_stream_shape",
    "check_cancel_mid_stream",
    "check_no_secrets_on_wire",
    "check_text_streams",
    "check_tool_call_streams_args",
    "check_usage_reported",
    "collect",
    "text_request",
    "tool_request",
]
