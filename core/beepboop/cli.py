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

import importlib
from importlib import metadata

import typer

from beepboop.providers.llm.cli import app as llm_app

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


def main() -> None:
    """Console-script entry point."""
    app()
