"""Step 03: augment and turn every WAV pool into microWakeWord feature mmaps.

Positives and near-misses get the full augmentation (RIR, background noise at
0 to 25 dB SNR, gain, pitch shift, EQ). Speech and non-speech negatives get the same
augmenter without the positive-specific jitter so the model never learns "reverb
means wake word". Ambient evaluation streams are converted unaugmented and unsplit;
the trainer slides over them to estimate false accepts per hour.

Output layout (what ``training.features[]`` in the trainer YAML points at)::

    features/positives/{training,validation,testing}/positives_mmap
    features/near_misses/...
    features/speech/{training,validation,testing}/<dataset_id>_mmap
    features/no_speech/...
    features/ambient/{validation_ambient,testing_ambient}/<dataset_id>_mmap
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from train_wakeword.config import PipelineConfig, Profile
from train_wakeword.log import get_logger
from train_wakeword.paths import Layout

log = get_logger(__name__)


def impulse_dirs(cfg: PipelineConfig, layout: Layout, profile: Profile) -> list[Path]:
    """RIR directories: every ``role: rir`` dataset plus the measured array IR, if present."""
    dirs = [layout.negative_wav(d.id) for d in cfg.datasets_for(profile, "rir")]
    if cfg.augment.array_ir_dir is not None:
        array_ir = Path(cfg.augment.array_ir_dir)
        if any(array_ir.glob("*.wav")):
            dirs.append(array_ir)
        else:
            log.warning(
                "augment.array_ir_pending",
                path=str(array_ir),
                note="XVF3800 sweep is an M0 owner action",
            )
    return [d for d in dirs if d.is_dir() and any(d.rglob("*.wav"))]


def background_dirs(cfg: PipelineConfig, layout: Layout, profile: Profile) -> list[Path]:
    """Background-noise directories: ``role: background`` and ``role: no_speech`` datasets."""
    dirs = [
        layout.negative_wav(d.id)
        for d in cfg.datasets_for(profile)
        if d.role in {"background", "no_speech"}
    ]
    return [d for d in dirs if d.is_dir() and any(d.rglob("*.wav"))]


def run(
    cfg: PipelineConfig, layout: Layout, profile: Profile, *, only: list[str] | None = None
) -> int:
    """Build every feature set; ``only`` restricts to a subset of set names."""
    from train_wakeword._vendor import build_ambient_mmaps, build_feature_mmaps

    irs = impulse_dirs(cfg, layout, profile)
    backgrounds = background_dirs(cfg, layout, profile)
    log.info(
        "augment.pools",
        impulse_dirs=[str(p) for p in irs],
        background_dirs=[str(p) for p in backgrounds],
    )
    if not irs or not backgrounds:
        log.error("augment.pools_missing", hint="run 02_fetch_negatives first")
        return 1
    step = cfg.training.window_step_ms
    seed = cfg.positives.seed
    wanted = set(only) if only else {"positives", "near_misses", "speech", "no_speech", "ambient"}
    report: dict[str, object] = {
        "profile": profile,
        "built_at": datetime.now(UTC).isoformat(),
        "impulse_dirs": [str(p) for p in irs],
        "background_dirs": [str(p) for p in backgrounds],
        "sets": {},
    }
    sets: dict[str, object] = {}

    if "positives" in wanted:
        sets["positives"] = build_feature_mmaps(
            clips_dir=layout.positives,
            out_dir=layout.feature_set("positives"),
            name="positives",
            step_ms=step,
            augment=cfg.augment,
            impulse_dirs=irs,
            background_dirs=backgrounds,
            duration_s=cfg.augment.duration_s,
            split_seed=seed,
            split_count=0.05,
            repeat_train=2,
            slide_frames=10,
            split_spectrogram_duration_s=None,
        )
    if "near_misses" in wanted and any(layout.near_misses.rglob("*.wav")):
        sets["near_misses"] = build_feature_mmaps(
            clips_dir=layout.near_misses,
            out_dir=layout.feature_set("near_misses"),
            name="near_misses",
            step_ms=step,
            augment=cfg.augment,
            impulse_dirs=irs,
            background_dirs=backgrounds,
            duration_s=cfg.augment.duration_s,
            split_seed=seed,
            split_count=0.05,
            repeat_train=2,
            slide_frames=10,
            split_spectrogram_duration_s=None,
        )
    for role in ("speech", "no_speech"):
        if role not in wanted:
            continue
        for ds in cfg.datasets_for(profile, role):
            wav_dir = layout.negative_wav(ds.id)
            if not wav_dir.is_dir():
                log.warning("augment.dataset_missing", dataset=ds.id)
                continue
            sets[f"{role}/{ds.id}"] = build_feature_mmaps(
                clips_dir=wav_dir,
                out_dir=layout.feature_set(role),
                name=ds.id,
                step_ms=step,
                augment=cfg.augment.model_copy(update={"min_jitter_s": 0.0, "max_jitter_s": 0.0}),
                impulse_dirs=irs,
                background_dirs=backgrounds,
                duration_s=None,
                split_seed=seed,
                split_count=0.05,
                repeat_train=1,
                slide_frames=1,
                split_spectrogram_duration_s=5.0,
            )
    if "ambient" in wanted:
        for ds in cfg.datasets_for(profile, "ambient_eval"):
            wav_dir = layout.negative_wav(ds.id)
            if not wav_dir.is_dir():
                log.warning("augment.dataset_missing", dataset=ds.id)
                continue
            sets[f"ambient/{ds.id}"] = build_ambient_mmaps(
                wav_dir=wav_dir,
                out_dir=layout.feature_set("ambient"),
                name=ds.id,
                step_ms=step,
                split_seed=seed,
            )
    report["sets"] = sets
    layout.features.mkdir(parents=True, exist_ok=True)
    (layout.features / "MANIFEST.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    log.info("augment.done", sets=list(sets))
    return 0
