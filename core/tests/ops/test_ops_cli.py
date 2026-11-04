"""``beepboop keys rotate-dek``, ``db backup`` / ``db restore`` and ``health`` on the CLI."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from beepboop import cli
from beepboop.db import Database, repo
from beepboop.db.backup import generate_recovery_key, set_recipients
from beepboop.secrets import SecretRef
from beepboop.service import CoreConfig, open_secrets

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    passphrase = tmp_path / "kek.passphrase"
    passphrase.write_text("correct horse\n")
    monkeypatch.setenv("BB_KEK_PASSPHRASE_FILE", str(passphrase))
    monkeypatch.setenv("BB_BACKUPS_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BB_HOST", "beepboop.local")
    monkeypatch.delenv("BB_HA_DIR", raising=False)
    data = tmp_path / "data"

    async def first_boot() -> None:
        cfg = replace(CoreConfig.from_env(), data_dir=data)
        db = await Database.open(cfg.db_path)
        try:
            store = await open_secrets(db, cfg)
            await store.put(SecretRef("ha_long_lived_token"), "tok")
        finally:
            await db.close()

    asyncio.run(first_boot())
    return data


def test_keys_rotate_dek(env: Path) -> None:
    result = runner.invoke(cli.app, ["keys", "rotate-dek", "--data-dir", str(env)])
    assert result.exit_code == 0, result.output
    assert "1 secrets re-encrypted" in result.output

    async def actions() -> list[str]:
        db = await Database.open(env / "beepboop.db")
        try:
            return await db.read(lambda c: [r.action for r in repo.audit.list_(c)])
        finally:
            await db.close()

    assert "keys.rotate_dek" in asyncio.run(actions())


def test_db_backup_then_restore(env: Path, tmp_path: Path) -> None:
    key = generate_recovery_key()
    result = runner.invoke(cli.app, ["db", "backup", "--data-dir", str(env)])
    assert result.exit_code == 1  # no recovery key yet: a recorded failure
    assert "FAILED" in result.output

    async def set_key() -> None:
        db = await Database.open(env / "beepboop.db")
        try:
            await db.write(lambda c: set_recipients(c, [key.recipient]), durable=True)
        finally:
            await db.close()

    asyncio.run(set_key())
    result = runner.invoke(cli.app, ["db", "backup", "--data-dir", str(env)])
    assert result.exit_code == 0, result.output
    bundle = next((tmp_path / "backups").glob("beepboop-beepboop.local-*.tar.age"))

    ident = tmp_path / "recovery.txt"
    ident.write_text(key.identity.get_secret_value())
    result = runner.invoke(
        cli.app,
        ["db", "restore", str(bundle), "--identity", str(ident), "--data-dir", str(env)],
    )
    assert result.exit_code == 0, result.output
    assert "1 keys re-wrapped" in result.output
    wrong = tmp_path / "wrong.txt"
    wrong.write_text(generate_recovery_key().identity.get_secret_value())
    result = runner.invoke(
        cli.app,
        ["db", "restore", str(bundle), "--identity", str(wrong), "--data-dir", str(env)],
    )
    assert result.exit_code == 2
    assert "does not open" in result.output


def test_health_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok(url: str, timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"}, request=httpx.Request("GET", url))

    def down(url: str, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", ok)
    assert runner.invoke(cli.app, ["health", "--quiet"]).exit_code == 0
    monkeypatch.setattr(httpx, "get", down)
    assert runner.invoke(cli.app, ["health", "--quiet"]).exit_code == 1
