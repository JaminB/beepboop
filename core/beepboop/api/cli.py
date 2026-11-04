"""``beepboop api openapi`` (WEB_UI §8): the document ``pnpm --dir ui gen:api`` consumes."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from beepboop import __version__
from beepboop.api.openapi import DEFAULT_OUT, openapi_document, write_openapi

app = typer.Typer(help="API tooling.", no_args_is_help=True)


@app.command()
def openapi(
    out: Annotated[Path, typer.Option("--out")] = DEFAULT_OUT,
    check_only: Annotated[bool, typer.Option("--check", help="Fail if the file is stale.")] = False,
) -> None:
    """Write /api/openapi.json to a file with sorted keys (CI diffs it)."""
    import json

    if check_only:
        current = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
        if current != openapi_document(__version__):
            typer.echo(f"{out} is stale; run `beepboop api openapi`", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"{out} is up to date")
        return
    typer.echo(f"wrote {write_openapi(out, __version__)}")


__all__ = ["app"]
