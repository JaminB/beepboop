"""`train-wakeword check` and the numbered wrapper scripts work in the workspace venv."""

from __future__ import annotations

import importlib.util
import py_compile
from pathlib import Path

import pytest

from train_wakeword.cli import build_parser, main
from train_wakeword.config import PipelineConfig
from train_wakeword.paths import Layout
from train_wakeword.positives import plan, split_counts

ROOT = Path(__file__).resolve().parents[1]


def test_check_passes(config_path: Path, licenses_path: Path) -> None:
    assert main(["--config", str(config_path), "--licenses", str(licenses_path), "check"]) == 0


def test_check_research_profile(config_path: Path) -> None:
    assert main(["--config", str(config_path), "--profile", "research", "check"]) == 0


def test_every_step_is_a_subcommand() -> None:
    parser = build_parser()
    subparsers = next(a for a in parser._actions if a.dest == "step")
    assert subparsers.choices is not None
    assert set(subparsers.choices) == {
        "check",
        "positives",
        "negatives",
        "augment",
        "train",
        "export",
        "evaluate",
    }


@pytest.mark.parametrize("script", sorted(p.name for p in (ROOT / "scripts").glob("0*_*.py")))
def test_wrapper_scripts_compile(script: str) -> None:
    py_compile.compile(str(ROOT / "scripts" / script), doraise=True)


def test_six_wrapper_scripts_exist() -> None:
    names = sorted(p.name for p in (ROOT / "scripts").glob("0*_*.py"))
    assert names == [
        "01_generate_positives.py",
        "02_fetch_negatives.py",
        "03_augment.py",
        "04_train.py",
        "05_export.py",
        "06_evaluate.py",
    ]


def testsplit_counts_sum_exactly() -> None:
    assert sum(split_counts(20000, [1, 1, 1, 1, 0.6])) == 20000
    assert split_counts(10, [1, 1, 1]) == [4, 3, 3]


def test_generation_plan(config: PipelineConfig, tmp_path: Path) -> None:
    layout = Layout(tmp_path, config.model_name)
    jobs = plan(config, layout, "shipped")
    positives = [j for j in jobs if j.label.startswith("positives/")]
    assert sum(j.count for j in positives) == config.positives.target_count
    assert {j.phoneme_input for j in positives} == {True, False}
    near = [j for j in jobs if j.label.startswith("near_misses/")]
    assert sum(j.count for j in near) == config.positives.near_miss_count
    argv = positives[0].argv(config, layout)
    assert argv[1:3] == ["-m", "piper_sample_generator"]
    assert "--length-scales" in argv
    assert "--model" in argv
    # one invocation never mixes .pt and .onnx models (the generator refuses that)
    for job in jobs:
        assert len({v.kind for v in job.voices}) == 1


@pytest.mark.skipif(
    importlib.util.find_spec("microwakeword") is None, reason="training deps not installed"
)
def test_vendor_matches_upstream_signatures() -> None:
    """Only runs in the training env: our adapter passes keywords the pinned trainer accepts."""
    from train_wakeword._vendor import upstream_init_params

    params = upstream_init_params()
    assert {
        "augmentation_duration_s",
        "augmentation_probabilities",
        "impulse_paths",
        "background_paths",
        "min_jitter_s",
    } <= params["Augmentation"]
    assert {"input_directory", "file_pattern", "random_split_seed", "split_count"} <= params[
        "Clips"
    ]
    assert {
        "clips",
        "augmenter",
        "step_ms",
        "slide_frames",
        "split_spectrogram_duration_s",
    } <= params["SpectrogramGeneration"]
