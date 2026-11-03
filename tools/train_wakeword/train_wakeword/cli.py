"""``train-wakeword`` / ``python -m train_wakeword``: one sub-command per pipeline step.

``check`` needs only pydantic and pyyaml and is what CI and the smoke tests run; the
other steps import the training libraries lazily when they start.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from train_wakeword.config import PipelineConfig, Profile
from train_wakeword.licenses import check_licenses, load_license_table
from train_wakeword.log import configure_logging, get_logger
from train_wakeword.paths import Layout

HERE = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = HERE / "config" / "hey_beepboop.yaml"
DEFAULT_LICENSES = HERE / "DATA_LICENSES.md"
DEFAULT_DATA = HERE / "data"

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="train-wakeword", description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--licenses", type=Path, default=DEFAULT_LICENSES)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument(
        "--profile",
        choices=("shipped", "research"),
        default="shipped",
        help=(
            "shipped: only licence-cleared voices/datasets (default); "
            "research: everything, never exportable"
        ),
    )
    sub = parser.add_subparsers(dest="step", required=True)

    sub.add_parser("check", help="validate the config and the licence table (no training deps)")

    p = sub.add_parser("positives", help="01: synthetic positives + near-misses (Piper)")
    p.add_argument("--dry-run", action="store_true", help="print the generator commands only")

    p = sub.add_parser("negatives", help="02: download + convert the negative datasets")
    p.add_argument("--only", nargs="+", metavar="DATASET_ID")

    p = sub.add_parser("augment", help="03: augment and write feature mmaps")
    p.add_argument(
        "--only", nargs="+", choices=("positives", "near_misses", "speech", "no_speech", "ambient")
    )

    p = sub.add_parser("train", help="04: run the microWakeWord trainer")
    p.add_argument(
        "--no-restore",
        action="store_true",
        help="start from scratch instead of the last checkpoint",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="write the YAML and print the command only"
    )

    p = sub.add_parser("export", help="05: copy the TFLite and write the manifest")
    p.add_argument(
        "--probability-cutoff",
        type=float,
        help="override export.probability_cutoff (from 06's recommendation)",
    )
    p.add_argument(
        "--allow-research",
        action="store_true",
        help="export a *_research model from research features",
    )

    p = sub.add_parser("evaluate", help="06: false accepts/hour + false rejects (JSON report)")
    p.add_argument("--tflite", type=Path, help="model to evaluate (default: out/<model>.tflite)")
    p.add_argument(
        "--ambient", type=Path, nargs="+", help="directories of 16 kHz WAV ambient streams"
    )
    p.add_argument(
        "--real-clips",
        type=Path,
        help="consented real 'Hey beepboop' clips, one sub-dir per speaker",
    )
    p.add_argument("--max-files", type=int, help="cap files per source (quick runs)")
    p.add_argument(
        "--require-targets",
        action="store_true",
        help="exit 1 if no operating point meets the targets",
    )
    return parser


def _check(cfg: PipelineConfig, licenses: Path) -> int:
    table = load_license_table(licenses)
    problems = check_licenses(cfg, table)
    for problem in problems:
        log.error("check.problem", detail=problem)
    log.info(
        "check.ok" if not problems else "check.failed",
        voices_shipped=len(cfg.voices("shipped")),
        voices_total=len(cfg.positives.voices),
        datasets_shipped=len(cfg.datasets_for("shipped")),
        datasets_total=len(cfg.datasets),
        licence_rows=len(table),
        problems=len(problems),
    )
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    config_path = cast(Path, args.config)
    cfg = PipelineConfig.load(config_path)
    profile = cast(Profile, args.profile)
    layout = Layout(cast(Path, args.data_dir), cfg.model_name)
    step = cast(str, args.step)

    if step == "check":
        return _check(cfg, cast(Path, args.licenses))
    if _check(cfg, cast(Path, args.licenses)) and profile == "shipped":
        return 1
    if step == "positives":
        from train_wakeword import positives

        return positives.run(cfg, layout, profile, dry_run=cast(bool, args.dry_run))
    if step == "negatives":
        from train_wakeword import negatives

        return negatives.run(cfg, layout, profile, only=cast("list[str] | None", args.only))
    if step == "augment":
        from train_wakeword import augment

        return augment.run(cfg, layout, profile, only=cast("list[str] | None", args.only))
    if step == "train":
        from train_wakeword import training

        return training.run(
            cfg, layout, restore=not cast(bool, args.no_restore), dry_run=cast(bool, args.dry_run)
        )
    if step == "export":
        from train_wakeword import export

        return export.run(
            cfg,
            config_path,
            layout,
            cast(Path, args.licenses),
            probability_cutoff=cast("float | None", args.probability_cutoff),
            allow_research=cast(bool, args.allow_research),
        )
    if step == "evaluate":
        from train_wakeword import evaluate

        return evaluate.run(
            cfg,
            layout,
            profile,
            tflite=cast("Path | None", args.tflite),
            ambient_dirs=cast("list[Path] | None", args.ambient),
            real_clips=cast("Path | None", args.real_clips),
            max_files=cast("int | None", args.max_files),
            require_targets=cast(bool, args.require_targets),
        )
    return 2  # pragma: no cover - argparse rejects unknown steps


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
