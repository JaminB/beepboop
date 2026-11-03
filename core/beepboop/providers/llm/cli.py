"""``beepboop llm ...``: developer commands for LLM providers.

Mounted by ``beepboop.cli``. Keep this module cheap to import (it loads on
every ``beepboop`` start): boto3 and the probe are imported inside commands.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="LLM provider tools (docs/LLM_PROVIDERS.md).", no_args_is_help=True)

#: Default result labels. A gate label is only ever written on the Pi (see ``--gate``).
PRELIMINARY = "preliminary; not a gate measurement"
GATE_LABEL = "gate measurement on the Pi 4, home connection (ROADMAP G3)"


@app.command()
def probe(
    profile: Annotated[
        str | None, typer.Option(help="AWS profile for ambient credentials (dev only).")
    ] = None,
    region: Annotated[
        str | None, typer.Option(help="AWS region; default: the profile's, else the manifest's.")
    ] = None,
    model: Annotated[
        str | None, typer.Option(help="Model id; default: manifest model_fast.")
    ] = None,
    pairs: Annotated[int, typer.Option(help="Warm tool-turn pairs (2 calls each).")] = 6,
    max_tokens: Annotated[int, typer.Option(help="max_tokens per call.")] = 60,
    cold_gap_minutes: Annotated[
        float, typer.Option(help="Wait this long, then one cold-cache pair (G3 needs >= 6).")
    ] = 0.0,
    host: Annotated[str, typer.Option(help="Host label for the result, e.g. pi4-4gb.")] = "dev",
    label: Annotated[
        str | None,
        typer.Option(help=f"Free-text label written into the result (default: {PRELIMINARY!r})."),
    ] = None,
    gate: Annotated[
        bool,
        typer.Option(
            help="Mark as a gate measurement. Refused off the Pi: ROADMAP G3 is measured on "
            "the Pi 4 on the home connection."
        ),
    ] = False,
    out: Annotated[
        Path | None, typer.Option(help="Write the JSON result here (else print it).")
    ] = None,
) -> None:
    """Measure Bedrock TTFT (M0 gate G3): warm, cold-TLS and cold-cache turns.

    Makes at most 20 short billed calls. Prints or writes the JSON result.
    """
    from beepboop.providers.llm.bedrock.probe import on_raspberry_pi

    if gate and not on_raspberry_pi():
        typer.echo(
            "refusing --gate: ROADMAP G3 is measured on the Pi 4 and this host is not a "
            "Raspberry Pi (Linux/aarch64 with a Raspberry Pi device tree). Drop --gate for a "
            "preliminary run; the result is then labelled as not a gate measurement.",
            err=True,
        )
        raise typer.Exit(code=2)
    if label is None:
        label = GATE_LABEL if gate else PRELIMINARY

    import boto3

    from beepboop.log import configure_logging
    from beepboop.providers.llm.bedrock.probe import (
        ProbeConfig,
        run_probe,
    )
    from beepboop.providers.llm.bedrock.provider import (
        BedrockProvider,
        BedrockSettings,
        make_client,
    )

    configure_logging(stream=sys.stderr)  # stdout carries the JSON result
    defaults = BedrockSettings.from_manifest(credentials="ambient")
    chosen_region = region or boto3.Session(profile_name=profile).region_name or defaults.region
    config = ProbeConfig(
        model_id=model or defaults.model_fast,
        region=chosen_region,
        pairs=pairs,
        max_tokens=max_tokens,
        cold_gap_s=cold_gap_minutes * 60,
    )

    def make_provider() -> BedrockProvider:
        return BedrockProvider(make_client(chosen_region, profile=profile), config.model_id)

    result = asyncio.run(
        run_probe(
            config,
            make_provider,
            label=label,
            host=host,
            measurement="gate" if gate else "preliminary",
        )
    )
    text = json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out} ({result.calls_made} calls, access={result.access})", err=True)
    if result.access != "ok":
        raise typer.Exit(code=1)
