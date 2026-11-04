"""The v0.1 dev-kit commands (CLAUDE.md §9): module check/schema/new, api openapi, db migrate."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from beepboop import cli
from beepboop.registry import cli as module_cli

runner = CliRunner()
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "docs" / "module-template"


def test_module_check_template_passes() -> None:
    result = runner.invoke(
        cli.app, ["module", "check", str(TEMPLATE_DIR), "--show-card", "--rubric"]
    )
    assert result.exit_code == 0, result.output
    assert "evals ok" in result.output
    assert "- parcel — " in result.output
    assert "rubric:" in result.output


def test_module_check_json_and_llm_not_wired() -> None:
    result = runner.invoke(cli.app, ["module", "check", str(TEMPLATE_DIR), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    result = runner.invoke(cli.app, ["module", "check", str(TEMPLATE_DIR), "--llm"])
    assert result.exit_code == 2


def test_module_schema_check_is_current() -> None:
    result = runner.invoke(cli.app, ["module", "schema", "--check"])
    assert result.exit_code == 0, result.output


def test_module_new_scaffolds_and_checks(tmp_path: Path, monkeypatch: object) -> None:
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setattr(module_cli, "MODULES_DIR", tmp_path / "modules")
    result = runner.invoke(cli.app, ["module", "new", "pool_pump", "--ha", "--scope", "household"])
    assert result.exit_code == 0, result.output
    assert (
        (tmp_path / "modules" / "pool_pump" / "AGENT.md")
        .read_text(encoding="utf-8")
        .startswith("# pool_pump")
    )
    result = runner.invoke(cli.app, ["module", "new", "pool_pump"])
    assert result.exit_code != 0  # already exists


def test_api_openapi_writes_and_checks(tmp_path: Path) -> None:
    out = tmp_path / "openapi.json"
    result = runner.invoke(cli.app, ["api", "openapi", "--out", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    result = runner.invoke(cli.app, ["api", "openapi", "--out", str(out), "--check"])
    assert result.exit_code == 0


def test_db_migrate_and_policy_explain(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["db", "migrate", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "schema version 2" in result.output
    modules = tmp_path / "modules"
    modules.mkdir()
    # Migration 0002 applied the Guest preset: read + timers (IDENTITY_AND_TRUST §9).
    result = runner.invoke(
        cli.app,
        ["policy", "explain", "home.state", "--as", "guest", "--data-dir", str(tmp_path),
         "--modules-dir", str(modules), "--risk", "read"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "decision: allow" in result.output
    assert "not registered; assuming --risk" in result.output  # no modules loaded
    assert "preset:guest" in result.output
    # A registered tool uses its declared tier and the HA target extractor: unlocking a
    # lock is explained as critical/confirm for an adult, not as the tool's act tier.
    from beepboop.db import repo
    from beepboop.db.connect import connect
    from beepboop.db.repo.ha_entities import EntityRow
    from beepboop.policy import Grant

    conn = connect(tmp_path / "beepboop.db")
    repo.profiles.insert(conn, "Alex", "admin", "critical", profile_id="alex")
    repo.logins.insert(conn, "alex", "alex", "$argon2id$fixture")
    repo.ha_entities.upsert(
        conn,
        EntityRow("u-door", "lock.front_door", "lock", "active", friendly_name="Front door"),
    )
    repo.grants.insert(conn, Grant("g-tools", "profile", "alex", "tool_glob", "home.*", "critical"))
    repo.grants.insert(conn, Grant("g-door", "profile", "alex", "ha_entity", "u-door", "critical"))
    conn.commit()
    conn.close()
    result = runner.invoke(
        cli.app,
        ["policy", "explain", "home.turn_off", '{"target": {"name": "front door"}}', "--as",
         "alex", "--data-dir", str(tmp_path)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "module ha, declared act" in result.output
    assert "target: u-door lock.unlock" in result.output
    assert "decision: confirm  risk: critical" in result.output
    result = runner.invoke(
        cli.app, ["db", "backup", "--data-dir", str(tmp_path), "--out", str(tmp_path / "snap.db")]
    )
    assert result.exit_code == 0
    assert (tmp_path / "snap.db").exists()
