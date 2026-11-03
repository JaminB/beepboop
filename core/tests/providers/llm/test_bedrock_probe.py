"""The G3 probe runs every scenario on fakes and never invents numbers."""

from __future__ import annotations

import asyncio
import json
import platform
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from beepboop import cli
from beepboop import log as log_module
from beepboop.providers.llm import TextDelta, ToolCallEnd, ToolCallStart, Usage
from beepboop.providers.llm.bedrock import probe as probe_module
from beepboop.providers.llm.bedrock.fake import FakeConverseClient, RawEvent
from beepboop.providers.llm.bedrock.probe import (
    CORE_TOOLS,
    ProbeConfig,
    ProbeResult,
    assistant_parts,
    cost_usd,
    first_request,
    percentile,
    run_probe,
    second_request,
)
from beepboop.providers.llm.bedrock.provider import BedrockProvider

MODEL = "global.anthropic.claude-haiku-5-5"
NOW = datetime(2026, 11, 2, 7, 15, tzinfo=UTC)


class TurnClient(FakeConverseClient):
    """Answers call 1 with a tool call and call 2 (has a toolResult) with text."""

    def __init__(self, tool: list[RawEvent], text: list[RawEvent], **kwargs: Any) -> None:
        super().__init__([tool], **kwargs)  # Any: FakeConverseClient options
        self._text = text
        self._tool = tool

    def converse_stream(self, **kwargs: Any) -> Any:  # Any: test double
        has_result = "toolResult" in json.dumps(kwargs.get("messages", []))
        self._responses = [self._text if has_result else self._tool]
        return super().converse_stream(**kwargs)


def _unused_factory() -> BedrockProvider:
    raise AssertionError("the budget check must fail before any provider is built")


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_probe_runs_every_scenario_within_the_call_budget(
    tool_events: list[RawEvent], text_events: list[RawEvent]
) -> None:
    clients: list[TurnClient] = []

    def make() -> BedrockProvider:
        clients.append(TurnClient(tool_events, text_events))
        return BedrockProvider(clients[-1], MODEL)

    config = ProbeConfig(model_id=MODEL, region="us-west-2", pairs=2, cold_gap_s=360)
    result = await run_probe(
        config, make, label="test", host="ci", now=lambda: NOW, sleep=_no_sleep
    )

    assert result.access == "ok"
    assert result.calls_made == 1 + 2 + 2 * 2 + 2 + 2
    assert result.calls_made == sum(len(c.calls) for c in clients)
    assert result.calls_made <= config.max_calls
    scenarios = [s.scenario for s in result.samples]
    assert scenarios.count("warm_first") == 2
    assert scenarios.count("warm_second_tool_result") == 3  # incl. the cache-write pair
    assert {"cold_tls_fresh_client", "prewarmed_fresh_client", "cold_cache_second"} <= set(
        scenarios
    )
    assert all(s.error is None for s in result.samples)
    assert result.summary["warm_first"]["ttft_ms"].n == 2
    assert result.not_run == []
    assert result.measurement == "preliminary"
    first = next(s for s in result.samples if s.scenario == "warm_first")
    assert first.tool_called == "home.turn_off"
    assert first.cost_usd is not None


async def test_probe_records_skipped_cold_cache_and_does_not_sleep(
    tool_events: list[RawEvent], text_events: list[RawEvent]
) -> None:
    async def forbidden_sleep(_seconds: float) -> None:
        raise AssertionError("must not sleep without cold_gap_s")

    config = ProbeConfig(model_id=MODEL, region="us-west-2", pairs=1)
    result = await run_probe(
        config,
        lambda: BedrockProvider(TurnClient(tool_events, text_events), MODEL),
        label="test",
        host="ci",
        sleep=forbidden_sleep,
    )
    assert any("cold_cache" in item for item in result.not_run)


async def test_probe_stops_after_a_failed_healthcheck() -> None:
    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Converse")
    config = ProbeConfig(model_id=MODEL, region="us-west-2")
    result = await run_probe(
        config,
        lambda: BedrockProvider(FakeConverseClient([[]], error=denied), MODEL),
        label="test",
        host="ci",
    )
    assert result.access.startswith("no model access")
    assert result.calls_made == 1
    assert result.samples == []


async def test_failed_call_is_recorded_without_timings(tool_events: list[RawEvent]) -> None:
    throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "Cs")

    class FlakyClient(FakeConverseClient):
        def converse(self, **kwargs: Any) -> Any:  # Any: test double
            self.calls.append(dict(kwargs))
            return {"stopReason": "max_tokens"}

    config = ProbeConfig(model_id=MODEL, region="us-west-2", pairs=1)
    result = await run_probe(
        config,
        lambda: BedrockProvider(FlakyClient([tool_events], error=throttled), MODEL),
        label="test",
        host="ci",
    )
    assert result.samples
    assert all(s.ttft_ms is None and s.error and "Throttled" in s.error for s in result.samples)
    assert result.summary == {
        "cold_tls_cache_write": {},
        "cold_tls_fresh_client": {},
        "prewarmed_fresh_client": {},
        "warm_first": {},
    }


def test_call_budget_is_enforced() -> None:
    config = ProbeConfig(model_id=MODEL, region="us-west-2", pairs=9, cold_gap_s=360)
    with pytest.raises(ValueError, match="max_calls"):
        asyncio.run(run_probe(config, _unused_factory, label="x", host="x"))


def test_probe_prompt_layout() -> None:
    req = first_request(NOW, max_tokens=60, turn_id="t")
    assert [b.cache for b in req.system] == [False, False, True, False]
    assert "07:15" in req.system[-1].text
    assert [t.name for t in req.tools] == sorted(t.name for t in CORE_TOOLS)
    assert len(req.tools) == 12


def test_assistant_parts_replays_the_real_call_or_a_synthetic_one() -> None:
    events = [
        ToolCallStart(id="t9", name="home.turn_off"),
        ToolCallEnd(id="t9", input={"target": "kitchen"}),
    ]
    parts, tool_id = assistant_parts(events, 1)
    assert tool_id == "t9"
    assert parts[0].type == "tool_use"
    parts, tool_id = assistant_parts([TextDelta(text="Sure.")], 2)
    assert tool_id == "tooluse_probe_2"
    assert [p.type for p in parts] == ["text", "tool_use"]
    second = second_request(first_request(NOW, 60, "t"), parts, tool_id)
    assert second.messages[-1].content[0].type == "tool_result"
    assert second.metadata.kind == "followup"


def test_percentile_and_cost() -> None:
    assert percentile([1.0], 95) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([0.0, 10.0], 95) == pytest.approx(9.5)
    with pytest.raises(ValueError, match="no values"):
        percentile([], 50)
    price = probe_module.load_pricing()[MODEL]
    usage = Usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000)
    assert cost_usd(usage, price) == pytest.approx(price["in"] + price["cache_read"])
    assert cost_usd(usage, None) is None


def _no_logging_setup(**_kwargs: object) -> None:
    return None


def test_cli_probe_writes_json(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def fake_run_probe(config: ProbeConfig, make: Any, **kwargs: Any) -> ProbeResult:
        seen.update(kwargs)  # Any: run_probe keyword arguments
        return ProbeResult(
            label=kwargs["label"],
            measurement=kwargs["measurement"],
            host=kwargs["host"],
            started_at=NOW,
            environment={},
            config=config,
            access="ok",
        )

    monkeypatch.setattr(probe_module, "run_probe", fake_run_probe)
    # The CLI points structlog at sys.stderr; CliRunner's stderr is closed after
    # invoke, so keep the global logging config untouched for later tests.
    monkeypatch.setattr(log_module, "configure_logging", _no_logging_setup)
    out = tmp_path / "result.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "llm",
            "probe",
            "--region",
            "us-west-2",
            "--pairs",
            "1",
            "--host",
            "ws",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    written = json.loads(out.read_text("utf-8"))
    assert written["config"]["model_id"] == MODEL
    assert written["measurement"] == "preliminary"
    assert written["label"] == "preliminary; not a gate measurement"
    assert seen["host"] == "ws"


def test_cli_gate_is_refused_off_the_pi(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``gate`` label can only be written on the Pi (ROADMAP G3; never fake a gate result)."""
    called = False

    async def fake_run_probe(*_args: Any, **_kwargs: Any) -> ProbeResult:
        nonlocal called
        called = True
        raise AssertionError("run_probe must not run")

    monkeypatch.setattr(probe_module, "run_probe", fake_run_probe)
    monkeypatch.setattr(probe_module, "on_raspberry_pi", lambda: False)
    monkeypatch.setattr(log_module, "configure_logging", _no_logging_setup)
    result = CliRunner().invoke(cli.app, ["llm", "probe", "--gate", "--region", "us-west-2"])
    assert result.exit_code == 2
    assert "refusing --gate" in result.output
    assert not called


def test_cli_gate_on_the_pi_writes_a_gate_label(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run_probe(config: ProbeConfig, make: Any, **kwargs: Any) -> ProbeResult:
        return ProbeResult(
            label=kwargs["label"],
            measurement=kwargs["measurement"],
            host=kwargs["host"],
            started_at=NOW,
            environment={},
            config=config,
            access="ok",
        )

    monkeypatch.setattr(probe_module, "run_probe", fake_run_probe)
    monkeypatch.setattr(probe_module, "on_raspberry_pi", lambda: True)
    monkeypatch.setattr(log_module, "configure_logging", _no_logging_setup)
    out = tmp_path / "gate.json"
    result = CliRunner().invoke(
        cli.app,
        ["llm", "probe", "--gate", "--region", "us-west-2", "--host", "pi4-4gb", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    written = json.loads(out.read_text("utf-8"))
    assert written["measurement"] == "gate"
    assert written["label"].startswith("gate measurement on the Pi 4")


def test_on_raspberry_pi_needs_linux_aarch64_and_a_pi_device_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe_module, "board_model", lambda: "Raspberry Pi 4 Model B Rev 1.5")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    assert probe_module.on_raspberry_pi()
    monkeypatch.setattr(probe_module, "board_model", lambda: None)
    assert not probe_module.on_raspberry_pi()
    monkeypatch.setattr(probe_module, "board_model", lambda: "Raspberry Pi 4 Model B Rev 1.5")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert not probe_module.on_raspberry_pi()
