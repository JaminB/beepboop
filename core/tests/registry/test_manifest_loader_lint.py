"""Manifest model, schema file, discovery and the lint on the worked example."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from beepboop.registry import (
    SCHEMA_PATH,
    ManifestError,
    discover,
    lint_module,
    load_manifest,
    manifest_schema,
    parse_manifest,
)
from beepboop.registry.cli import scaffold
from beepboop.registry.manifest import schema_text
from beepboop.testing import run_evals

from .conftest import MANIFEST_YAML, TEMPLATE_DIR


def test_template_manifest_loads() -> None:
    m = load_manifest(TEMPLATE_DIR)
    assert m.id == "parcel"
    assert not m.ha_backed
    assert m.scope == "profile"
    track = m.tool("parcel.track")
    assert track is not None
    assert track.untrusted_output


def test_schema_file_is_current() -> None:
    assert SCHEMA_PATH.exists(), "run `beepboop module schema`"
    assert SCHEMA_PATH.read_text(encoding="utf-8") == schema_text()
    assert manifest_schema()["$schema"].endswith("2020-12/schema")


def test_manifest_rejects_missing_required() -> None:
    with pytest.raises(ManifestError, match="untrusted_output"):
        parse_manifest(
            MANIFEST_YAML.replace("    untrusted_output: false\n    args:", "    args:", 1)
        )
    with pytest.raises(ManifestError, match="api"):
        parse_manifest(MANIFEST_YAML.replace("api: 1", "api: 2", 1))


def test_manifest_rules() -> None:
    with pytest.raises(ManifestError, match="additionalProperties"):
        parse_manifest(
            MANIFEST_YAML.replace(
                "additionalProperties: false\n    output: {type: object, properties: {speak:"
                " {type: string}}}\n   "
                " examples",
                "output: {type: object, properties: {speak: {type: string}}}\n    examples",
                1,
            )
        )
    with pytest.raises(ManifestError, match="L13"):
        parse_manifest(
            MANIFEST_YAML.replace(
                "risk: critical\n    untrusted_output: false",
                "risk: critical\n    untrusted_output: true",
            )
        )
    with pytest.raises(ManifestError, match="folder"):
        parse_manifest(MANIFEST_YAML, folder_name="other")
    with pytest.raises(ManifestError, match="oauth"):
        parse_manifest(MANIFEST_YAML + "\noauth: {flow: bounce}\n")


def test_template_passes_lint_and_evals() -> None:
    report = lint_module(TEMPLATE_DIR)
    assert report.ok, [str(f) for f in report.errors]
    evals = run_evals(TEMPLATE_DIR)
    assert evals.ok, [(r.case_id, r.messages) for r in evals.failed]
    assert sum(r.ran_handler for r in evals.results) >= 5


def test_lint_catches_banned_import_and_missing_handler(tmp_path: Path) -> None:
    dest = tmp_path / "parcel"
    shutil.copytree(TEMPLATE_DIR, dest)
    module_py = dest / "module.py"
    module_py.write_text(
        module_py.read_text(encoding="utf-8")
        .replace(
            "from dataclasses import dataclass",
            "import boto3  # banned\nfrom dataclasses import dataclass",
        )
        .replace('@tool("parcel.remove")', '@tool("parcel.other")', 1),
        encoding="utf-8",
    )
    report = lint_module(dest)
    rules = {f.rule for f in report.errors}
    assert {"L9", "L2"} <= rules


def test_discover_and_scaffold(tmp_path: Path) -> None:
    modules = tmp_path / "modules"
    dest = scaffold("pool_pump", scope="household", category="home", ha=True, modules_dir=modules)
    assert (dest / "manifest.yaml").read_text(encoding="utf-8").startswith("# manifest.yaml")
    manifest = load_manifest(dest)
    assert manifest.id == "pool_pump"
    assert manifest.ha_backed
    assert manifest.scope == "household"
    assert manifest.tool_names == ["pool_pump.track", "pool_pump.add", "pool_pump.remove"]
    found = discover(modules)
    assert [f.id for f in found] == ["pool_pump"]
    assert found[0].error is None
    assert found[0].module_class is not None
    (modules / "broken").mkdir()
    (modules / "broken" / "manifest.yaml").write_text("id: broken\n", encoding="utf-8")
    broken = [f for f in discover(modules) if f.module_dir.name == "broken"]
    assert broken
    assert broken[0].error
