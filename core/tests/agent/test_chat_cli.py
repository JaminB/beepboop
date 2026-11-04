"""``beepboop chat --as`` over the real service wiring (CLAUDE.md §9)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from beepboop.agent.cli import run_chat
from beepboop.agent.phrases import PHRASES, SETUP_NEEDED
from beepboop.cli import app


async def test_chat_runs_turns_through_the_service(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    modules = tmp_path / "modules"
    modules.mkdir()
    code = await run_chat(
        "guest",
        ["what time is it", "", "what's the weather"],
        data_dir=tmp_path / "data",
        modules_dir=modules,
        dry_run=True,
        trace=True,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "beepboop> It's" in out  # fast path, no LLM
    assert f"beepboop> {SETUP_NEEDED}" in out  # no credentials: fast path only
    assert "why: Heard from" in out
    assert PHRASES["offline"] not in out


async def test_chat_unknown_profile(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    modules = tmp_path / "modules"
    modules.mkdir()
    code = await run_chat(
        "nobody",
        ["hi"],
        data_dir=tmp_path / "data",
        modules_dir=modules,
        dry_run=False,
        trace=False,
    )
    assert code == 2
    assert "no profile" in capsys.readouterr().err


def test_chat_is_registered() -> None:
    result = CliRunner().invoke(app, ["chat", "--help"])
    assert result.exit_code == 0
    assert "--as" in result.output
