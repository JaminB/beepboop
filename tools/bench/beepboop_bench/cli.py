"""Typer app mounted by core as ``beepboop bench``.

Keep this module cheap to import: core imports it every time the ``beepboop``
CLI starts. numpy, sherpa-onnx, boto3 and the bench modules are imported
*inside* the command functions, never at module level.

Subcommands are exactly OPERATIONS §11 / CLAUDE.md §9: speaker, stt, tts, wake,
aec, endpoint, llm, e2e, ram, thermal, all [--gates] [--json]. Every run
writes ``tools/bench/results/<date>-<host>.json`` and prints the tables;
``--json`` also prints the JSON. ``--gates`` is valid on ``all`` only and exits
non-zero unless every gate passes; it refuses to judge anywhere but a
Raspberry Pi with a real corpus unless ``--allow-non-pi`` (development only;
the file is then labelled "not a gate result").
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

import typer

app = typer.Typer(
    help="Pipeline benchmark and M0 go/no-go gates (results go to tools/bench/results/).",
    no_args_is_help=False,
)

_CORPUS_HELP = "Corpus folder with manifest.yaml (default tools/bench/corpus/)."
_MODELS_HELP = "Models root (default models/ or $BB_MODELS_DIR)."

Corpus = Annotated[Path | None, typer.Option("--corpus", help=_CORPUS_HELP)]
Models = Annotated[Path | None, typer.Option("--models", help=_MODELS_HELP)]
Threads = Annotated[int, typer.Option(help="Intra-op threads per model (G1/G2: 2).")]
Host = Annotated[str | None, typer.Option(help="Host label for the result file, e.g. pi4-4gb.")]
Out = Annotated[
    Path | None, typer.Option(help="Write the result here instead of results/<date>-<host>.json.")
]
Json = Annotated[bool, typer.Option("--json", help="Also print the result JSON to stdout.")]
Label = Annotated[str | None, typer.Option(help="Extra free-text label stored in the result.")]
MaxClips = Annotated[int | None, typer.Option(help="Use only the first N clips (smoke runs).")]
LlmLive = Annotated[
    bool,
    typer.Option("--llm-live", "--live", help="Call Bedrock for real (about 20 billed calls)."),
]
RamFrom = Annotated[
    list[Path] | None,
    typer.Option(
        "--ram-from",
        help="Earlier result file whose 24 h RAM profile(s) are merged into this run, so G4 "
        "sees both `default` and `music` (same host and RAM variant; repeatable).",
    ),
]
HaRunning = Annotated[
    bool | None,
    typer.Option(
        "--ha-running/--no-ha-running",
        help="Record whether Home Assistant was running (gate precondition).",
    ),
]
Offline = Annotated[
    bool | None,
    typer.Option(
        "--offline/--online",
        help="Record whether the network was unplugged for the fast-path turns.",
    ),
]
Concurrency = Annotated[
    int, typer.Option(help="Concurrent turns in e2e (emulates household load).")
]
Realtime = Annotated[
    bool,
    typer.Option(
        "--realtime/--no-realtime",
        help="Feed audio at real-time pace (default) or as fast as possible.",
    ),
]
MaxTurns = Annotated[int | None, typer.Option(help="Stop e2e after N turns (cycling the corpus).")]
Duration = Annotated[
    float | None, typer.Option(help="Run e2e for this many seconds (ram/thermal soaks).")
]


def _run(
    subs: Sequence[str],
    *,
    corpus: Path | None,
    models: Path | None,
    threads: int = 2,
    host: str | None = None,
    out: Path | None = None,
    json_out: bool = False,
    label: str | None = None,
    gates: bool = False,
    allow_non_pi: bool = False,
    max_clips: int | None = None,
    llm_live: bool = False,
    llm_pairs: int = 6,
    llm_cold_gap_minutes: float = 0.0,
    ha_running: bool | None = None,
    offline: bool | None = None,
    concurrency: int = 2,
    realtime: bool = True,
    max_turns: int | None = None,
    duration_s: float | None = None,
    aec_device: str | None = None,
    ram_profile: Literal["default", "music"] = "default",
    ram_from: Sequence[Path] = (),
    concurrent_stt: bool = True,
) -> None:
    from beepboop.log import configure_logging
    from beepboop_bench import run as runner
    from beepboop_bench.corpus import default_corpus_root
    from beepboop_bench.models import models_root

    configure_logging()
    opts = runner.Options(
        corpus=corpus or default_corpus_root(),
        models=models or models_root(),
        threads=threads,
        host=host,
        out=out,
        gates=gates,
        allow_non_pi=allow_non_pi,
        concurrency=concurrency,
        realtime=realtime,
        max_clips=max_clips,
        max_turns=max_turns,
        duration_s=duration_s,
        llm_live=llm_live,
        llm_pairs=llm_pairs,
        llm_cold_gap_s=llm_cold_gap_minutes * 60.0,
        ha_running=ha_running,
        offline=offline,
        aec_device=aec_device,
        ram_profile=ram_profile,
        ram_from=tuple(ram_from),
        label=label,
        concurrent_stt=concurrent_stt,
    )
    try:
        result = runner.run([s for s in runner.SUBS if s in subs], opts)
    except (runner.GateRefusedError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    path = runner.write_result(runner.result_path(opts, result, result.host), result)
    runner.render(result)
    typer.echo(f"wrote {path}", err=True)
    if json_out:
        typer.echo(result.to_json(), nl=False)
    if gates and (result.gates is None or not result.gates.all_passed):
        raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def bench(ctx: typer.Context) -> None:
    """Pipeline benchmark and M0 go/no-go gates (OPERATIONS §11, ROADMAP M0)."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


@app.command()
def speaker(
    corpus: Corpus = None,
    models: Models = None,
    threads: Threads = 2,
    host: Host = None,
    out: Out = None,
    json_out: Json = False,
    label: Label = None,
    max_clips: MaxClips = None,
    no_concurrent_stt: Annotated[
        bool, typer.Option("--no-concurrent-stt", help="Skip the STT load thread (G1 wants it on).")
    ] = False,
) -> None:
    """CAM++ embed latency p50/p95 for 1 s and 2 s clips, fp32 (int8 if exported); EER (G1)."""
    _run(
        ["speaker"],
        corpus=corpus,
        models=models,
        threads=threads,
        host=host,
        out=out,
        json_out=json_out,
        label=label,
        max_clips=max_clips,
        concurrent_stt=not no_concurrent_stt,
    )


@app.command()
def stt(
    corpus: Corpus = None,
    models: Models = None,
    host: Host = None,
    out: Out = None,
    json_out: Json = False,
    label: Label = None,
    max_clips: MaxClips = None,
) -> None:
    """zipformer RTF at 1 and 2 threads, final-tail latency, WER with and without hotwords (G2)."""
    _run(
        ["stt"],
        corpus=corpus,
        models=models,
        host=host,
        out=out,
        json_out=json_out,
        label=label,
        max_clips=max_clips,
    )


@app.command()
def tts(
    models: Models = None,
    threads: Threads = 2,
    host: Host = None,
    out: Out = None,
    json_out: Json = False,
    label: Label = None,
) -> None:
    """First-chunk latency per voice and RTF (G6 stage check)."""
    _run(
        ["tts"],
        corpus=None,
        models=models,
        threads=threads,
        host=host,
        out=out,
        json_out=json_out,
        label=label,
    )


@app.command()
def wake(
    corpus: Corpus = None,
    models: Models = None,
    host: Host = None,
    out: Out = None,
    json_out: Json = False,
    label: Label = None,
) -> None:
    """False accepts per 24 h on the soak corpus, false rejects at 1 m / 3 m, latency, CPU (G5)."""
    _run(["wake"], corpus=corpus, models=models, host=host, out=out, json_out=json_out, label=label)


@app.command()
def aec(
    models: Models = None,
    device: Annotated[
        str | None, typer.Option(help="ALSA device of the XVF3800, e.g. hw:1 (Linux only).")
    ] = None,
    host: Host = None,
    out: Out = None,
    json_out: Json = False,
    label: Label = None,
) -> None:
    """Wake-hit rate and STT WER during playback vs silence (V3). Needs the array and a person."""
    _run(
        ["aec"],
        corpus=None,
        models=models,
        host=host,
        out=out,
        json_out=json_out,
        label=label,
        aec_device=device,
    )


@app.command()
def endpoint(
    corpus: Corpus = None,
    models: Models = None,
    host: Host = None,
    out: Out = None,
    json_out: Json = False,
    label: Label = None,
) -> None:
    """Truncation and over-run across VAD hangover values on labelled command ends (G6)."""
    _run(
        ["endpoint"],
        corpus=corpus,
        models=models,
        host=host,
        out=out,
        json_out=json_out,
        label=label,
    )


@app.command()
def llm(
    live: LlmLive = False,
    pairs: Annotated[int, typer.Option(help="Warm tool-turn pairs (2 calls each).")] = 6,
    cold_gap_minutes: Annotated[
        float, typer.Option(help="Wait this long, then one cold-cache pair (G3 needs >= 6).")
    ] = 0.0,
    host: Host = None,
    out: Out = None,
    json_out: Json = False,
    label: Label = None,
) -> None:
    """Bedrock TTFT p50/p95, first and second (cached) call, warm vs cold (G3). Needs --llm-live."""
    _run(
        ["llm"],
        corpus=None,
        models=None,
        host=host,
        out=out,
        json_out=json_out,
        label=label,
        llm_live=live,
        llm_pairs=pairs,
        llm_cold_gap_minutes=cold_gap_minutes,
    )


@app.command()
def e2e(
    corpus: Corpus = None,
    models: Models = None,
    threads: Threads = 2,
    concurrency: Concurrency = 2,
    realtime: Realtime = True,
    max_turns: MaxTurns = None,
    duration: Duration = None,
    live: LlmLive = False,
    ha_running: HaRunning = None,
    offline: Offline = None,
    host: Host = None,
    out: Out = None,
    json_out: Json = False,
    label: Label = None,
) -> None:
    """Full pipeline replayed concurrently: waterfall per turn, p50/p90/p95 per scenario (G6)."""
    _run(
        ["e2e"],
        corpus=corpus,
        models=models,
        threads=threads,
        host=host,
        out=out,
        json_out=json_out,
        label=label,
        llm_live=live,
        ha_running=ha_running,
        offline=offline,
        concurrency=concurrency,
        realtime=realtime,
        max_turns=max_turns,
        duration_s=duration,
    )


@app.command()
def ram(
    corpus: Corpus = None,
    models: Models = None,
    duration: Duration = None,
    profile: Annotated[
        str, typer.Option(help="default or music (G4 needs both, 24 h each).")
    ] = "default",
    concurrency: Concurrency = 2,
    live: LlmLive = False,
    ram_from: RamFrom = None,
    host: Host = None,
    out: Out = None,
    json_out: Json = False,
    label: Label = None,
) -> None:
    """Peak (MemTotal - MemAvailable) at 1 Hz during e2e, zram used, OOM kills (G4)."""
    if profile not in ("default", "music"):
        typer.echo("--profile must be default or music", err=True)
        raise typer.Exit(code=2)
    _run(
        ["ram"],
        corpus=corpus,
        models=models,
        host=host,
        out=out,
        json_out=json_out,
        label=label,
        llm_live=live,
        concurrency=concurrency,
        duration_s=duration,
        ram_profile="music" if profile == "music" else "default",
        ram_from=ram_from or (),
    )


@app.command()
def thermal(
    corpus: Corpus = None,
    models: Models = None,
    duration: Annotated[
        float, typer.Option(help="Seconds of e2e under the thermal sampler (G7: 1200).")
    ] = 1200.0,
    concurrency: Concurrency = 2,
    live: LlmLive = False,
    host: Host = None,
    out: Out = None,
    json_out: Json = False,
    label: Label = None,
) -> None:
    """SoC temperature and throttle bits during a 20-minute e2e (G7)."""
    _run(
        ["thermal"],
        corpus=corpus,
        models=models,
        host=host,
        out=out,
        json_out=json_out,
        label=label,
        llm_live=live,
        concurrency=concurrency,
        duration_s=duration,
    )


@app.command(name="all")
def all_(
    corpus: Corpus = None,
    models: Models = None,
    gates: Annotated[
        bool, typer.Option("--gates", help="Judge G1-G7 and exit non-zero on any failure.")
    ] = False,
    allow_non_pi: Annotated[
        bool,
        typer.Option(
            "--allow-non-pi",
            help="Development only: judge gates off the Pi; the file says 'not a gate result'.",
        ),
    ] = False,
    threads: Threads = 2,
    concurrency: Concurrency = 2,
    realtime: Realtime = True,
    max_clips: MaxClips = None,
    max_turns: MaxTurns = None,
    duration: Duration = None,
    live: LlmLive = False,
    ha_running: HaRunning = None,
    offline: Offline = None,
    aec_device: Annotated[
        str | None, typer.Option(help="ALSA device for bench aec (Linux only).")
    ] = None,
    ram_from: RamFrom = None,
    host: Host = None,
    out: Out = None,
    json_out: Json = False,
    label: Label = None,
) -> None:
    """Everything above into results/<date>-<host>.json; --gates exits non-zero on a failure."""
    _run(
        list(_ALL),
        corpus=corpus,
        models=models,
        threads=threads,
        host=host,
        out=out,
        json_out=json_out,
        label=label,
        gates=gates,
        allow_non_pi=allow_non_pi,
        max_clips=max_clips,
        llm_live=live,
        ha_running=ha_running,
        offline=offline,
        concurrency=concurrency,
        realtime=realtime,
        max_turns=max_turns,
        duration_s=duration,
        aec_device=aec_device,
        ram_from=ram_from or (),
    )


_ALL: tuple[str, ...] = (
    "speaker",
    "stt",
    "tts",
    "wake",
    "aec",
    "endpoint",
    "llm",
    "e2e",
    "ram",
    "thermal",
)
