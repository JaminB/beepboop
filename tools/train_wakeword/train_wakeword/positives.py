"""Step 01: synthetic positives and TTS near-misses with the Piper sample generator.

Each phrase variant is rendered by (a) the 904-speaker LibriTTS-R generator and (b)
the single-speaker Piper voices, cycling length scales (speed) and noise scales.
Pitch variation happens in step 03 (``PitchShift``), because Piper has no pitch
control. A deterministic ``holdout_fraction`` of every phrase directory is moved to
``positives_holdout/`` and never trained on; step 06 measures false rejects on it.

The generator is run as a subprocess from the pinned clone (``make deps``) because
the PyPI wheel omits ``piper_train``, which the ``.pt`` generator needs.
"""

from __future__ import annotations

import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from train_wakeword.config import PipelineConfig, Profile, Voice
from train_wakeword.fetch import download
from train_wakeword.log import get_logger
from train_wakeword.paths import Layout

log = get_logger(__name__)


@dataclass(frozen=True)
class GenerationJob:
    """One sample-generator invocation."""

    label: str
    prompt: str  # text, phoneme string, or a path to a file of lines
    phoneme_input: bool
    voices: tuple[Voice, ...]
    count: int
    output_dir: Path

    def argv(self, cfg: PipelineConfig, layout: Layout) -> list[str]:
        """Command line for ``python -m piper_sample_generator``."""
        pos = cfg.positives
        argv = [
            sys.executable,
            "-m",
            "piper_sample_generator",
            self.prompt,
            "--max-samples",
            str(self.count),
            "--batch-size",
            str(pos.batch_size),
            "--output-dir",
            str(self.output_dir),
            "--length-scales",
            *(str(x) for x in pos.length_scales),
            "--noise-scales",
            *(str(x) for x in pos.noise_scales),
            "--noise-scale-ws",
            *(str(x) for x in pos.noise_scale_ws),
        ]
        for voice in self.voices:
            argv += ["--model", str(layout.models / voice.filename)]
        max_speakers = min((v.max_speakers for v in self.voices if v.max_speakers), default=None)
        if max_speakers is not None:
            argv += ["--max-speakers", str(max_speakers)]
        if self.phoneme_input:
            argv.append("--phoneme-input")
        return argv


def split_counts(total: int, weights: list[float]) -> list[int]:
    """Largest-remainder split of ``total`` by ``weights`` (sums exactly to ``total``)."""
    scale = total / sum(weights)
    raw = [w * scale for w in weights]
    counts = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - counts[i], reverse=True)[
        : total - sum(counts)
    ]:
        counts[i] += 1
    return counts


def plan(cfg: PipelineConfig, layout: Layout, profile: Profile) -> list[GenerationJob]:
    """Every generator invocation for the positives and the near-misses."""
    pos = cfg.positives
    voices = cfg.voices(profile)
    generators = tuple(v for v in voices if v.kind == "generator")
    pipers = tuple(v for v in voices if v.kind == "piper")
    groups: list[tuple[str, tuple[Voice, ...], float]] = []
    if generators:
        groups.append(("generator", generators, pos.generator_share if pipers else 1.0))
    if pipers:
        groups.append(("piper", pipers, (1.0 - pos.generator_share) if generators else 1.0))
    if not groups:
        msg = f"no voices available under profile {profile!r}"
        raise ValueError(msg)

    jobs: list[GenerationJob] = []
    per_phrase = split_counts(pos.target_count, [p.weight for p in pos.phrases])
    for phrase, phrase_count in zip(pos.phrases, per_phrase, strict=True):
        per_group = split_counts(phrase_count, [share for _, _, share in groups])
        for (group, group_voices, _), count in zip(groups, per_group, strict=True):
            if count == 0:
                continue
            jobs.append(
                GenerationJob(
                    label=f"positives/{phrase.id}/{group}",
                    prompt=phrase.text if phrase.text is not None else str(phrase.phonemes),
                    phoneme_input=phrase.phonemes is not None,
                    voices=group_voices,
                    count=count,
                    output_dir=layout.positives / phrase.id / group,
                )
            )
    if pos.near_miss_count:
        prompts = layout.near_misses / "prompts.txt"
        per_group = split_counts(pos.near_miss_count, [share for _, _, share in groups])
        for (group, group_voices, _), count in zip(groups, per_group, strict=True):
            if count:
                jobs.append(
                    GenerationJob(
                        label=f"near_misses/{group}",
                        prompt=str(prompts),
                        phoneme_input=False,
                        voices=group_voices,
                        count=count,
                        output_dir=layout.near_misses / group,
                    )
                )
    return jobs


def ensure_voices(cfg: PipelineConfig, layout: Layout, profile: Profile) -> None:
    """Download every voice file (and the ``.onnx.json`` next to each Piper voice)."""
    layout.models.mkdir(parents=True, exist_ok=True)
    for voice in cfg.voices(profile):
        target = layout.models / voice.filename
        download(voice.url, target)
        if voice.kind == "piper":
            download(voice.url + ".json", target.with_name(target.name + ".json"))


def hold_out(cfg: PipelineConfig, layout: Layout) -> int:
    """Move a seeded ``holdout_fraction`` of each phrase/group directory to the holdout dir."""
    rng = random.Random(cfg.positives.seed)  # noqa: S311 # reproducible split, not security
    moved = 0
    for phrase in cfg.positives.phrases:
        for group_dir in sorted((layout.positives / phrase.id).glob("*")):
            if not group_dir.is_dir():
                continue
            files = sorted(group_dir.glob("*.wav"))
            rng.shuffle(files)
            keep = int(len(files) * cfg.positives.holdout_fraction)
            target = layout.positives_holdout / phrase.id / group_dir.name
            target.mkdir(parents=True, exist_ok=True)
            for wav in files[:keep]:
                wav.replace(target / wav.name)
                moved += 1
    return moved


def run(cfg: PipelineConfig, layout: Layout, profile: Profile, *, dry_run: bool = False) -> int:
    """Generate positives and near-misses; return 0 on success."""
    if not (layout.sample_generator / "piper_sample_generator").is_dir():
        log.error(
            "sample_generator.missing", path=str(layout.sample_generator), hint="run `make deps`"
        )
        return 1
    jobs = plan(cfg, layout, profile)
    log.info("positives.plan", jobs=len(jobs), total=sum(j.count for j in jobs), profile=profile)
    if dry_run:
        for job in jobs:
            log.info("positives.job", label=job.label, count=job.count, argv=job.argv(cfg, layout))
        return 0
    ensure_voices(cfg, layout, profile)
    layout.near_misses.mkdir(parents=True, exist_ok=True)
    (layout.near_misses / "prompts.txt").write_text(
        "\n".join(cfg.positives.near_misses) + "\n", encoding="utf-8"
    )
    for job in jobs:
        if job.output_dir.exists() and len(list(job.output_dir.glob("*.wav"))) >= job.count:
            log.info("positives.skip_existing", label=job.label)
            continue
        job.output_dir.mkdir(parents=True, exist_ok=True)
        log.info("positives.generate", label=job.label, count=job.count, voices=len(job.voices))
        subprocess.run(job.argv(cfg, layout), cwd=layout.sample_generator, check=True)
    if any(layout.positives_holdout.glob("*/*/*.wav")):
        log.info("positives.holdout_exists", path=str(layout.positives_holdout))
    else:
        log.info("positives.holdout", moved=hold_out(cfg, layout))
    return 0
