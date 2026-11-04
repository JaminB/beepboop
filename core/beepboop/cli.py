"""The ``beepboop`` command line.

CLAUDE.md §9 is the command registry: build commands with exactly those names,
and add a row there when you add one. Commands are thin; real work lives in
the subpackages so it can be tested without the CLI.

``beepboop bench ...`` is provided by the optional ``beepboop-bench`` workspace
package (``tools/bench``). Core never depends on it: if it is installed its
Typer app is mounted as the ``bench`` sub-command, otherwise ``bench`` explains
how to install it and exits non-zero.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer

from beepboop.agent.cli import chat
from beepboop.api.cli import app as api_app
from beepboop.db.cli import app as db_app
from beepboop.paths import default_data_dir
from beepboop.policy.cli import app as policy_app
from beepboop.providers.llm.cli import app as llm_app
from beepboop.registry.cli import app as module_app
from beepboop.secrets.cli import app as keys_app

#: Evaluated once per process (B008): BB_DATA_DIR, /data in the container, else ./.data.
DATA_DIR_DEFAULT = default_data_dir()

if TYPE_CHECKING:
    from beepboop.service import CoreConfig

#: Distributions reported by ``beepboop version``.
DISTRIBUTIONS = ("beepboop", "beepboop-audio", "beepboop-bench")

#: Module that must expose ``app: typer.Typer`` for ``beepboop bench``.
BENCH_MODULE = "beepboop_bench.cli"

app = typer.Typer(
    name="beepboop",
    help="beepboop: a Raspberry Pi home voice assistant that knows who is speaking.",
    no_args_is_help=True,
    add_completion=False,
)


def installed_version(distribution: str) -> str:
    """Return the installed version of ``distribution``, or ``"not installed"``."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "not installed"


@app.command()
def version() -> None:
    """Print the installed beepboop package versions."""
    for distribution in DISTRIBUTIONS:
        typer.echo(f"{distribution} {installed_version(distribution)}")


def _bench_not_installed() -> typer.Typer:
    """A stand-in ``bench`` group used when beepboop-bench is not installed."""
    stub = typer.Typer(help="Pipeline benchmark and M0 gates (beepboop-bench is not installed).")

    @stub.callback(invoke_without_command=True)
    def not_installed() -> None:
        typer.echo(
            "beepboop bench needs the beepboop-bench package; run `uv sync --all-packages`.",
            err=True,
        )
        raise typer.Exit(code=1)

    return stub


def load_bench_app() -> typer.Typer:
    """Import ``beepboop_bench.cli.app`` if the package is installed.

    Only a missing ``beepboop_bench`` falls back to the stub; any other import
    error inside the bench package propagates so it is never silently hidden.
    """
    try:
        module = importlib.import_module(BENCH_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name not in {"beepboop_bench", BENCH_MODULE}:
            raise
        return _bench_not_installed()
    bench_app: object = getattr(module, "app", None)
    if not isinstance(bench_app, typer.Typer):
        msg = f"{BENCH_MODULE}.app must be a typer.Typer, got {type(bench_app).__name__}"
        raise TypeError(msg)
    return bench_app


app.add_typer(load_bench_app(), name="bench")
app.add_typer(llm_app, name="llm")
app.add_typer(module_app, name="module")
app.add_typer(db_app, name="db")
app.add_typer(policy_app, name="policy")
app.add_typer(api_app, name="api")
app.add_typer(keys_app, name="keys")
app.command(name="chat")(chat)


@app.command()
def health(
    quiet: Annotated[bool, typer.Option("--quiet", help="No output; exit code only.")] = False,
    url: Annotated[str | None, typer.Option(help="Default: core's own bind address.")] = None,
    timeout: Annotated[float, typer.Option(help="Seconds.")] = 4.0,
) -> None:
    """Health probe for the compose healthcheck: GET /api/health (OPERATIONS §11)."""
    import os

    import httpx

    target = url or (
        f"http://{os.environ.get('BB_BIND_HOST', '127.0.0.1')}:"
        f"{os.environ.get('BB_BIND_PORT', '8080')}/api/health"
    )
    try:
        response = httpx.get(target, timeout=timeout)
        body: object = response.json()
        status = cast(dict[str, object], body).get("status") if isinstance(body, dict) else None
        ok = response.status_code == 200 and status == "ok"
    except (httpx.HTTPError, ValueError) as exc:
        if not quiet:
            typer.echo(f"unhealthy: {type(exc).__name__}", err=True)
        raise typer.Exit(code=1) from exc
    if not quiet:
        typer.echo(response.text)
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def serve(
    data_dir: Annotated[Path, typer.Option("--data-dir", envvar="BB_DATA_DIR")] = DATA_DIR_DEFAULT,
    modules_dir: Annotated[
        Path | None, typer.Option("--modules-dir", envvar="BB_MODULES_DIR")
    ] = None,
    host: Annotated[str, typer.Option(envvar="BB_BIND_HOST")] = "127.0.0.1",
    port: Annotated[int, typer.Option(envvar="BB_BIND_PORT")] = 8080,
) -> None:
    """Run core: API + SPA, agent, registry, scheduler, and the audio connection.

    Everything not given as an option comes from the ``BB_*`` environment
    (``CoreConfig.from_env``), including ``BB_AUDIO_SOCKET`` and ``BB_HA_URL``.
    """
    from beepboop.log import configure_logging
    from beepboop.service import serve as run_serve

    configure_logging()
    asyncio.run(run_serve(serve_config(data_dir, modules_dir, host, port)))


def serve_config(
    data_dir: Path,
    modules_dir: Path | None,
    host: str,
    port: int,
    env: dict[str, str] | None = None,
) -> CoreConfig:
    """``CoreConfig.from_env`` with the ``serve`` options laid over it."""
    from dataclasses import replace

    from beepboop.service import CoreConfig as Config

    base = Config.from_env(env)
    return replace(
        base,
        data_dir=data_dir,
        modules_dir=modules_dir or base.modules_dir,
        bind_host=host,
        bind_port=port,
    )


ca_app = typer.Typer(help="The Tier-0 local certificate authority (WEB_UI §7).")


@ca_app.command("fingerprint")
def ca_fingerprint_cmd(
    cert: Annotated[
        Path | None,
        typer.Option("--cert", envvar="BB_CA_CERT", help="Root certificate (PEM or DER)."),
    ] = None,
) -> None:
    """Print the root CA fingerprint the device speaks after the claim code (/ca helper)."""
    from beepboop.api.routers.system import fingerprint_of

    found = fingerprint_of(cert)
    if found is None:
        typer.echo("no root certificate: pass --cert or set BB_CA_CERT", err=True)
        raise typer.Exit(code=1)
    typer.echo(found)


app.add_typer(ca_app, name="ca")


def main() -> None:
    """Console-script entry point."""
    # Findings and help text use "§" and arrows; a cp1252 console (Windows) must not crash.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    app()
