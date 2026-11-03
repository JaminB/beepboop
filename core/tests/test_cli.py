"""Tests for the ``beepboop`` CLI skeleton."""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest
import typer
from typer.testing import CliRunner

from beepboop import cli

runner = CliRunner()


def test_version_lists_every_workspace_package() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert [line.split()[0] for line in lines] == list(cli.DISTRIBUTIONS)
    assert "not installed" not in result.output


def test_installed_version_of_missing_distribution() -> None:
    assert cli.installed_version("beepboop-does-not-exist") == "not installed"


def test_bench_is_mounted_when_installed() -> None:
    result = runner.invoke(cli.app, ["bench"])
    assert result.exit_code == 0
    assert "M0" in result.output


def _fail_bench_import(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """Make importing beepboop_bench raise ModuleNotFoundError for ``missing``."""
    real_import_module = importlib.import_module

    def fake_import_module(name: str, package: str | None = None) -> ModuleType:
        if name.startswith("beepboop_bench"):
            raise ModuleNotFoundError(f"No module named {missing!r}", name=missing)
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)


def test_bench_stub_when_package_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_bench_import(monkeypatch, "beepboop_bench")
    app = typer.Typer()
    app.add_typer(cli.load_bench_app(), name="bench")

    result = runner.invoke(app, ["bench"])
    assert result.exit_code == 1
    assert "uv sync --all-packages" in result.output


def test_bench_import_errors_inside_package_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_bench_import(monkeypatch, "sherpa_onnx")
    with pytest.raises(ModuleNotFoundError, match="sherpa_onnx"):
        cli.load_bench_app()
