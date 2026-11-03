"""``beepboop bench``: exactly the OPERATIONS §11 subcommands, refusal exit codes, cheap import."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from beepboop_bench.cli import app
from beepboop_bench.corpus import CorpusManifest, write_manifest

#: OPERATIONS §11 / CLAUDE.md §9, in that order.
DOCUMENTED = [
    "speaker",
    "stt",
    "tts",
    "wake",
    "aec",
    "endpoint",
    "llm",
    "e2e",
    "ram",
    "thermal",
    "all",
]


def test_bench_without_subcommand_prints_help() -> None:
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0
    assert "M0" in result.output


def test_subcommands_are_exactly_the_documented_ones() -> None:
    names = [
        info.name or (info.callback.__name__ if info.callback else "?")
        for info in app.registered_commands
    ]
    assert names == DOCUMENTED


def test_gates_only_on_all() -> None:
    for name in DOCUMENTED[:-1]:
        result = CliRunner().invoke(app, [name, "--gates"])
        assert result.exit_code != 0, name
        assert "No such option" in result.output, name


def test_all_gates_refuses_off_pi_with_synthetic_corpus(tmp_path: Path) -> None:
    write_manifest(
        tmp_path, CorpusManifest(name="t", synthetic=True, recorded_through="synthetic-tts")
    )
    result = CliRunner().invoke(app, ["all", "--gates", "--corpus", str(tmp_path)])
    assert result.exit_code == 2
    assert "refusing to evaluate gates" in result.output


def test_cli_module_import_is_cheap() -> None:
    """core imports beepboop_bench.cli on every start: no numpy / sherpa at import time."""
    code = (
        "import sys; import beepboop_bench.cli; "
        "heavy = [m for m in ('numpy', 'sherpa_onnx', 'boto3', 'pymicro_wakeword') "
        "if m in sys.modules]; "
        "print(heavy)"
    )
    out = subprocess.run(  # noqa: S603 fixed interpreter and code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"
