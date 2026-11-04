"""``beepboop keys rotate-dek`` (CLAUDE.md §9, DATA_AND_PRIVACY §6.3)."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from beepboop.paths import default_data_dir

#: Evaluated once per process (B008): BB_DATA_DIR, /data in the container, else ./.data.
DATA_DIR_DEFAULT = default_data_dir()


app = typer.Typer(help="Envelope-encryption keys.", no_args_is_help=True)


@app.command("rotate-dek")
def rotate_dek(
    data_dir: Annotated[
        Path, typer.Option("--data-dir", envvar="BB_DATA_DIR", help="Core data root.")
    ] = DATA_DIR_DEFAULT,
) -> None:
    """Create a new active data key and re-encrypt every secret under it, in one transaction."""
    from beepboop.db.database import Database
    from beepboop.service import CoreConfig, open_secrets

    config = replace(CoreConfig.from_env(), data_dir=data_dir)

    async def _run() -> tuple[str, int]:
        db = await Database.open(config.db_path, backups_dir=config.backups_path)
        try:
            store = await open_secrets(db, config)
            dek_id = await store.rotate_dek()
            count = await db.read(_count_secrets)
            await db.write(lambda c: _audit(c, dek_id, count), durable=True)
            return dek_id, count
        finally:
            await db.close()

    dek_id, count = asyncio.run(_run())
    typer.echo(f"active data key {dek_id}; {count} secrets re-encrypted")


def _count_secrets(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM secrets").fetchone()
    return int(row[0]) if row else 0


def _audit(conn: sqlite3.Connection, dek_id: str, count: int) -> None:
    from beepboop.db.repo import audit

    audit.record(
        conn,
        actor_id=None,
        actor_name="system",
        via="system",
        action="keys.rotate_dek",
        target=dek_id,
        details={"secrets": count},
    )


__all__ = ["app"]
