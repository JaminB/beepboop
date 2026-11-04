"""Module-author DX regressions from the v0.1 review: `module new` TODO markers and option
validation, lint findings instead of tracebacks (YAML errors, syntax errors, type
mismatches, unknown eval tools), eval output validation, the rubric line, the fake LLM
and `chat --seed`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beepboop import cli
from beepboop.registry import cli as module_cli
from beepboop.registry.lint import lint_module
from beepboop.registry.manifest import ManifestError, parse_manifest
from beepboop.registry.rubric import auto_rubric
from beepboop.testing.evals import run_evals
from beepboop.testing.llm import EvalsProvider

runner = CliRunner()
REPO = Path(__file__).resolve().parents[2]


def _scaffold(tmp_path: Path, **kw: object) -> Path:
    return module_cli.scaffold("toyping", modules_dir=tmp_path / "modules", **kw)  # type: ignore[arg-type]


def test_scaffold_leaves_todo_markers_and_passes_check(tmp_path: Path) -> None:
    dest = _scaffold(tmp_path, scope="household", category="home", ha=True)
    todos = {n: (dest / n).read_text(encoding="utf-8").count("TODO") for n in (
        "manifest.yaml", "module.py", "AGENT.md", "SETUP.md", "evals.yaml"
    )}  # fmt: skip
    assert all(n >= 1 for n in todos.values()), todos
    manifest = (dest / "manifest.yaml").read_text(encoding="utf-8")
    assert "household: one connection" in manifest  # the scope comment follows --scope
    assert "profile: each person connects" not in manifest
    report = lint_module(dest)
    assert report.ok, [str(f) for f in report.errors]
    assert not report.warnings, [str(f) for f in report.warnings]  # AGENT.md under 80 %
    assert run_evals(dest).ok


def test_scaffold_validates_options_before_writing(tmp_path: Path) -> None:
    import typer

    with pytest.raises(typer.BadParameter, match="--scope"):
        _scaffold(tmp_path, scope="bogus")
    with pytest.raises(typer.BadParameter, match="--category"):
        _scaffold(tmp_path, category="bogus")
    with pytest.raises(typer.BadParameter, match="lowercase"):
        module_cli.scaffold("Bad-Id", modules_dir=tmp_path / "modules")
    assert not (tmp_path / "modules").exists()


def test_module_new_prints_next_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module_cli, "MODULES_DIR", tmp_path / "modules")
    result = runner.invoke(cli.app, ["module", "new", "pool_pump"])
    assert result.exit_code == 0, result.output
    assert "evals ok" in result.output
    assert "next steps:" in result.output
    assert "grep -n TODO" in result.output
    assert "module check" in result.output
    result = runner.invoke(cli.app, ["module", "new", "--help"])
    assert "lowercase letters" in result.output
    assert "{1,31}" not in result.output


def test_yaml_and_syntax_errors_are_findings_not_tracebacks(tmp_path: Path) -> None:
    dest = _scaffold(tmp_path)
    manifest = dest / "manifest.yaml"
    good = manifest.read_text(encoding="utf-8")
    manifest.write_text(good + "\n  bad: [unclosed\n", encoding="utf-8")
    report = lint_module(dest)
    assert not report.ok
    assert any(
        f.rule == "L1" and "not valid YAML" in f.message and "line" in f.message
        for f in report.errors
    )
    manifest.write_text(good, encoding="utf-8")
    module_py = dest / "module.py"
    module_py.write_text(
        module_py.read_text(encoding="utf-8") + "\ndef broken(:\n", encoding="utf-8"
    )
    report = lint_module(dest)
    assert any(
        f.rule == "L2" and "syntax error" in f.message and "line" in f.message
        for f in report.errors
    )
    with pytest.raises(ManifestError) as exc:
        parse_manifest(good.replace("untrusted_output: false", "untrusted_ouput: false", 1))
    assert "unknown key 'untrusted_ouput'" in str(exc.value)
    assert "errors.pydantic.dev" not in str(exc.value)
    assert "manifest.yaml tools[" in str(exc.value)


def test_handler_type_mismatch_and_unknown_eval_tool_are_lint_errors(tmp_path: Path) -> None:
    dest = _scaffold(tmp_path)
    module_py = dest / "module.py"
    src = module_py.read_text(encoding="utf-8")
    assert "tracking_number: str | None = None" in src
    module_py.write_text(
        src.replace("tracking_number: str | None = None", "tracking_number: int | None = None"),
        encoding="utf-8",
    )
    report = lint_module(dest)
    assert any(f.rule == "L2" and "handler type" in f.message for f in report.errors)
    module_py.write_text(src, encoding="utf-8")
    evals = dest / "evals.yaml"
    text = evals.read_text(encoding="utf-8")
    evals.write_text(text.replace("tool: toyping.track", "tool: toyping.trak", 1), encoding="utf-8")
    report = lint_module(dest)
    assert any(f.rule == "L8" and "unknown tool 'toyping.trak'" in f.message for f in report.errors)


def test_eval_runner_reports_handler_crashes_and_bad_output(tmp_path: Path) -> None:
    dest = _scaffold(tmp_path)
    module_py = dest / "module.py"
    src = module_py.read_text(encoding="utf-8")
    # The first statement of the first handler body: crash it.
    # (`parcel` became `toyping` everywhere in the scaffold, variables included.)
    marker = "        toypings = await self._load(ctx)\n        if tracking_number:"
    assert marker in src
    crashed = src.replace(marker, "        _ = 1 // 0\n" + marker, 1)
    assert crashed != src
    module_py.write_text(crashed, encoding="utf-8")
    result = run_evals(dest)
    assert not result.ok
    messages = [m for r in result.failed for m in r.messages]
    assert any("handler raised ZeroDivisionError at module.py:" in m for m in messages), messages
    # A result without `speak` fails even when the case has no speak_contains.
    module_py.write_text(src, encoding="utf-8")
    nospeak = re.sub(
        r'return \{"speak": [^\n]*?"toypings": \[[^\n]*\]\}',
        'return {"toypings": [p.to_dict() for p in fresh]}',
        src,
    )
    if nospeak == src:
        pytest.skip("template return shape changed; update this test")
    module_py.write_text(nospeak, encoding="utf-8")
    result = run_evals(dest)
    assert any("no non-empty 'speak'" in m for r in result.failed for m in r.messages)


def test_rubric_prints_one_total(tmp_path: Path) -> None:
    dest = _scaffold(tmp_path)
    report = lint_module(dest)
    assert report.manifest is not None
    line = auto_rubric(dest, report.manifest, evals_ok=True)
    assert re.match(r"rubric: \d+/16 so far \(bar 12\)", line)
    assert "pending R2, R3, R6" in line
    result = runner.invoke(cli.app, ["module", "check", str(dest), "--rubric"])
    assert result.exit_code == 0, result.output
    assert "/16 so far" in result.output


def test_fake_llm_picks_the_tool_from_evals() -> None:
    provider = EvalsProvider.from_modules(REPO / "modules")
    offered = {"weather.current", "find_tools", "caldav.events"}
    choice = provider.pick("what's the weather like", offered)
    assert choice is not None
    assert choice[0] == "weather.current"
    assert provider.pick("zzz qqq", offered) is None


async def test_chat_seed_and_fake_llm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from beepboop.agent.cli import run_chat

    code = await run_chat(
        "alex",
        ["what's the weather"],
        data_dir=tmp_path / "data",
        modules_dir=REPO / "modules",
        dry_run=True,
        trace=False,
        seed=True,
        fake_llm=True,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "seeded Alex, Sam, Robin" in out
    assert "fake LLM" in out
    assert "beepboop>" in out
    assert "finish setup" not in out  # the fake provider answered instead of SETUP_NEEDED


def test_module_list_has_a_header(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(cli.app, ["module", "list"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].startswith("module")
    assert "weather" in result.output
