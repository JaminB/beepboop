"""``beepboop db migrate | backup | restore`` (CLAUDE.md §9, DATA_AND_PRIVACY §5, §11).

``backup`` builds the encrypted bundle; ``backup --out FILE`` is steps 1-2 only (the
pre-update snapshot the host's ``beepboop update`` takes). ``restore`` is what the host's
``beepboop db restore`` runs in a one-off core container while core is stopped.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from beepboop import __version__
from beepboop.db.backup import (
    BackupError,
    BackupSources,
    restore_bundle,
    run_backup,
)
from beepboop.db.database import Database
from beepboop.paths import default_data_dir

#: Evaluated once per process (B008): BB_DATA_DIR, /data in the container, else ./.data.
DATA_DIR_DEFAULT = default_data_dir()


app = typer.Typer(help="Schema migrations, backups and restore.", no_args_is_help=True)

DataDir = Annotated[
    Path,
    typer.Option(
        "--data-dir", envvar="BB_DATA_DIR", help="Core data root (/data in the container)."
    ),
]


@app.command()
def migrate(data_dir: DataDir = DATA_DIR_DEFAULT) -> None:
    """Apply pending migrations (runs at core start before the API binds)."""

    async def _run() -> int:
        db = await Database.open(data_dir / "beepboop.db", backups_dir=data_dir / "backups")
        try:
            return await db.schema_version()
        finally:
            await db.close()

    typer.echo(f"schema version {asyncio.run(_run())}")


@app.command()
def backup(
    data_dir: DataDir = DATA_DIR_DEFAULT,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Snapshot only (steps 1-2) to this file; no bundle."),
    ] = None,
    recipient: Annotated[
        list[str] | None,
        typer.Option("--recipient", help="age1... recipient; default: the household's."),
    ] = None,
    destination: Annotated[
        list[Path] | None,
        typer.Option("--destination", help="Extra destination dir; default: the household's."),
    ] = None,
) -> None:
    """Write an encrypted backup bundle (DATA_AND_PRIVACY §11), or a bare snapshot with --out."""
    from beepboop.service import CoreConfig, open_secrets

    config = replace(CoreConfig.from_env(), data_dir=data_dir)

    async def _snapshot(target: Path) -> Path:
        db = await Database.open(config.db_path, migrate_schema=False)
        try:
            await db.checkpoint()
            await asyncio.to_thread(target.unlink, missing_ok=True)
            return await db.vacuum_into(target)
        finally:
            await db.close()

    if out is not None:
        typer.echo(f"snapshot {asyncio.run(_snapshot(out))}")
        return

    async def _bundle() -> tuple[bool, str]:
        db = await Database.open(config.db_path, backups_dir=config.backups_path)
        try:
            store = await open_secrets(db, config)
            src = BackupSources(
                db=db,
                secrets=store,
                keys_dir=config.keys_dir,
                backups_dir=config.backups_path,
                host=config.host,
                core_version=__version__,
                ha_dir=config.ha_dir,
            )
            result = await run_backup(
                src, now=datetime.now(UTC), recipients=recipient, destinations=destination
            )
            lines = [f"{'ok' if result.ok else 'FAILED'} {result.name} ({result.size} bytes)"]
            lines += [
                f"  {d.destination}: {'ok' if d.ok else d.error} (pruned {d.pruned})"
                for d in result.destinations
            ]
            return result.ok, "\n".join(lines)
        finally:
            await db.close()

    ok, report = asyncio.run(_bundle())
    typer.echo(report)
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def restore(
    bundle: Annotated[Path, typer.Argument(help="beepboop-<host>-<ts>.tar.age")],
    identity_file: Annotated[
        str,
        typer.Option(
            "--identity",
            help="File holding the recovery key (AGE-SECRET-KEY-1...), or - for stdin.",
        ),
    ] = "-",
    data_dir: DataDir = DATA_DIR_DEFAULT,
) -> None:
    """Restore a bundle onto this Pi. Core must be stopped; the host wrapper does that."""
    from beepboop.secrets import read_otp_key, read_passphrase_file, resolve_kek
    from beepboop.service import CoreConfig

    config = replace(CoreConfig.from_env(), data_dir=data_dir)
    identity = (
        sys.stdin.read()
        if identity_file == "-"
        else Path(identity_file).read_text(encoding="utf-8")
    )

    passphrase = (
        read_passphrase_file(config.kek_passphrase_file)
        if config.kek_passphrase_file is not None
        else None
    )
    try:
        report = restore_bundle(
            bundle.read_bytes(),
            identity,
            data_dir=config.data_dir,
            backups_dir=config.backups_path,
            ha_dir=config.ha_dir,
            # An existing wrapped KEK always wins; on a Pi where core never booted it is
            # created here, exactly as first boot would (DATA_AND_PRIVACY §6.3).
            kek=lambda: resolve_kek(
                config.keys_dir,
                otp_reader=read_otp_key,
                passphrase=passphrase,
                create_passphrase_file=True,
            ),
            now=datetime.now(UTC),
        )
    except BackupError as exc:
        typer.echo(f"restore refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"restored {report.host} backup from {report.created_at:%Y-%m-%d %H:%M}Z: schema "
        f"{report.user_version_from} -> {report.user_version_to}, {report.deks_imported} keys "
        f"re-wrapped, {report.ha_files} Home Assistant files"
    )
    if report.previous_db is not None:
        typer.echo(f"previous database kept at {report.previous_db}")


__all__ = ["app"]
