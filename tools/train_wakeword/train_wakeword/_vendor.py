# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""Adapters around the untyped, training-only libraries.

This is the only module that imports ``microwakeword``, ``mmap_ninja``,
``pymicro_wakeword``, ``soundfile`` or ``soxr``. None of them ships type
information and none is installed in the workspace venv, so this file is checked
in pyright *basic* mode (the reason for the header comment) and every import is
local to the function that needs it. Everything else in the package stays strict.

API notes (verified against the pinned commits, see pyproject.toml):

* ``microwakeword.audio.clips.Clips(input_directory, file_pattern, ...,
  random_split_seed, split_count)``; ``audio_generator(split=, repeat=)``.
* ``microwakeword.audio.augmentation.Augmentation(augmentation_duration_s,
  augmentation_probabilities, impulse_paths, background_paths, ...)``.
* ``microwakeword.audio.spectrograms.SpectrogramGeneration(clips, augmenter,
  step_ms, split_spectrogram_duration_s, slide_frames)``;
  ``spectrogram_generator(split=, repeat=)``.
* ``mmap_ninja.ragged.RaggedMmap.from_generator(out_dir, sample_generator,
  batch_size, verbose)``.
* ``pymicro_wakeword.MicroWakeWord(id, wake_word=, tflite_model=,
  probability_cutoff=, sliding_window_size=, trained_languages=)``;
  ``process_streaming_prob(features)``; ``MicroWakeWordFeatures().process_streaming(bytes)``.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from train_wakeword.config import AugmentConfig

SAMPLE_RATE = 16000
SAMPLES_PER_CHUNK = 160  # 10 ms, what pymicro-wakeword expects per call


def convert_audio_to_16k(src: Path, dst: Path) -> float:
    """Decode any soundfile-readable file, mix to mono, resample to 16 kHz PCM16.

    Returns the duration in seconds of the written file.
    """
    import soundfile as sf
    import soxr

    data, sr = sf.read(str(src), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != SAMPLE_RATE:
        mono = soxr.resample(mono, sr, SAMPLE_RATE, quality="HQ")
    pcm = np.clip(mono * 32767.0, -32768, 32767).astype(np.int16)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), pcm, SAMPLE_RATE, subtype="PCM_16")
    return len(pcm) / SAMPLE_RATE


def audio_duration_s(path: Path) -> float:
    """Duration of an audio file without decoding it."""
    import soundfile as sf

    info = sf.info(str(path))
    return float(info.frames) / float(info.samplerate)


def _augmenter(
    cfg: AugmentConfig,
    impulse_dirs: list[Path],
    background_dirs: list[Path],
    duration_s: float | None,
):
    from microwakeword.audio.augmentation import Augmentation

    return Augmentation(
        augmentation_duration_s=duration_s,
        augmentation_probabilities=dict(cfg.probabilities),
        impulse_paths=[str(p) for p in impulse_dirs],
        background_paths=[str(p) for p in background_dirs],
        background_min_snr_db=cfg.background_min_snr_db,
        background_max_snr_db=cfg.background_max_snr_db,
        min_gain_db=cfg.min_gain_db,
        max_gain_db=cfg.max_gain_db,
        min_jitter_s=cfg.min_jitter_s,
        max_jitter_s=cfg.max_jitter_s,
    )


def build_feature_mmaps(
    *,
    clips_dir: Path,
    out_dir: Path,
    name: str,
    step_ms: int,
    augment: AugmentConfig | None,
    impulse_dirs: list[Path],
    background_dirs: list[Path],
    duration_s: float | None,
    split_seed: int,
    split_count: float,
    repeat_train: int,
    slide_frames: int,
    split_spectrogram_duration_s: float | None,
    batch_size: int = 100,
) -> dict[str, int]:
    """Write ``out_dir/{training,validation,testing}/<name>_mmap`` from a WAV directory.

    Returns the number of spectrograms written per split.
    """
    from microwakeword.audio.clips import Clips
    from microwakeword.audio.spectrograms import SpectrogramGeneration
    from mmap_ninja.ragged import RaggedMmap

    clips = Clips(
        input_directory=str(clips_dir),
        file_pattern="**/*.wav",
        max_clip_duration_s=None,
        remove_silence=False,
        random_split_seed=split_seed,
        split_count=split_count,
    )
    augmenter = (
        _augmenter(augment, impulse_dirs, background_dirs, duration_s)
        if augment is not None
        else None
    )
    plan = (
        ("train", "training", repeat_train, slide_frames),
        ("validation", "validation", 1, slide_frames),
        ("test", "testing", 1, 1),
    )
    counts: dict[str, int] = {}
    for split, subdir, repeat, slide in plan:
        generation = SpectrogramGeneration(
            clips=clips,
            augmenter=augmenter,
            step_ms=step_ms,
            slide_frames=slide,
            split_spectrogram_duration_s=split_spectrogram_duration_s,
        )
        target = out_dir / subdir / f"{name}_mmap"
        target.parent.mkdir(parents=True, exist_ok=True)
        RaggedMmap.from_generator(
            out_dir=str(target),
            sample_generator=generation.spectrogram_generator(split=split, repeat=repeat),
            batch_size=batch_size,
            verbose=True,
        )
        counts[subdir] = len(RaggedMmap(str(target)))
    return counts


def build_ambient_mmaps(
    *, wav_dir: Path, out_dir: Path, name: str, step_ms: int, split_seed: int, batch_size: int = 20
) -> dict[str, int]:
    """Write full-length (unsplit) spectrograms to ``validation_ambient`` / ``testing_ambient``.

    The trainer slides its own window over these to estimate false accepts per hour,
    so they are never cut or augmented here. 45 % of the files go to each side; the
    remaining 10 % are unused (``Clips`` cannot hold out everything).
    """
    from microwakeword.audio.clips import Clips
    from microwakeword.audio.spectrograms import SpectrogramGeneration
    from mmap_ninja.ragged import RaggedMmap

    clips = Clips(
        input_directory=str(wav_dir),
        file_pattern="**/*.wav",
        random_split_seed=split_seed,
        split_count=0.45,
    )
    counts: dict[str, int] = {}
    for split, subdir in (("validation", "validation_ambient"), ("test", "testing_ambient")):
        generation = SpectrogramGeneration(clips=clips, augmenter=None, step_ms=step_ms)
        target = out_dir / subdir / f"{name}_mmap"
        target.parent.mkdir(parents=True, exist_ok=True)
        RaggedMmap.from_generator(
            out_dir=str(target),
            sample_generator=generation.spectrogram_generator(split=split),
            batch_size=batch_size,
            verbose=True,
        )
        counts[subdir] = len(RaggedMmap(str(target)))
    return counts


def tflite_c_library() -> Path:
    """The ``tensorflowlite_c`` shared library bundled in the pymicro-wakeword wheel.

    ``MicroWakeWord.__init__`` requires the path explicitly; only ``from_config`` resolves
    it, and ``from_config`` needs a manifest next to the model, which 06 may not have yet.
    """
    import pymicro_wakeword

    lib_dir = Path(pymicro_wakeword.__file__).parent / "lib"
    found = next(iter(lib_dir.glob("*tensorflowlite_c.*")), None)
    if found is None:
        msg = f"no tensorflowlite_c library in {lib_dir} (unsupported platform wheel?)"
        raise RuntimeError(msg)
    return found


class Scorer:
    """Stream WAV files through ``pymicro-wakeword`` and return raw per-inference probabilities.

    The model is loaded once. ``sliding_window_size=1`` makes ``process_streaming_prob``
    return the un-averaged probability of each inference, so 06_evaluate can sweep
    window sizes and cutoffs offline from a single pass. ``MicroWakeWord.reset()``
    reloads the interpreter between clips, so the streaming state never leaks across files.
    """

    def __init__(self, tflite: Path, wake_word: str) -> None:
        from pymicro_wakeword import MicroWakeWord

        self._mww = MicroWakeWord(
            tflite.stem,
            wake_word=wake_word,
            tflite_model=tflite,
            probability_cutoff=0.5,
            sliding_window_size=1,
            trained_languages=["en"],
            libtensorflowlite_c_path=tflite_c_library(),
        )
        self.stride: int = int(self._mww.stride)

    def score(self, wav: Path) -> np.ndarray:
        """Return the probability after each inference (one value per ``stride`` feature frames).

        ``process_streaming_prob`` returns 0.0 while it is still buffering ``stride``
        feature frames and runs one inference on the frame that completes a group, so
        only every ``stride``-th return value is a real probability.
        """
        from pymicro_wakeword import MicroWakeWordFeatures

        pcm = read_wav16k(wav)
        self._mww.reset()
        features = MicroWakeWordFeatures()
        frame_index = 0
        probs: list[float] = []
        for offset in range(0, len(pcm) - SAMPLES_PER_CHUNK + 1, SAMPLES_PER_CHUNK):
            chunk = pcm[offset : offset + SAMPLES_PER_CHUNK].tobytes()
            for feature in features.process_streaming(chunk):
                prob = self._mww.process_streaming_prob(feature)
                frame_index += 1
                if frame_index % self.stride == 0 and prob is not None:
                    probs.append(float(prob))
        return np.asarray(probs, dtype=np.float64)


def upstream_init_params() -> dict[str, set[str]]:
    """Constructor parameter names of the trainer classes this module calls (for the tests)."""
    import inspect

    from microwakeword.audio.augmentation import Augmentation
    from microwakeword.audio.clips import Clips
    from microwakeword.audio.spectrograms import SpectrogramGeneration

    return {
        name: set(inspect.signature(cls.__init__).parameters)
        for name, cls in (
            ("Augmentation", Augmentation),
            ("Clips", Clips),
            ("SpectrogramGeneration", SpectrogramGeneration),
        )
    }


def read_wav16k(path: Path) -> np.ndarray:
    """Read a 16 kHz mono PCM16 WAV into an int16 array (raises on any other format)."""
    with wave.open(str(path), "rb") as wf:
        if (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            msg = f"{path}: expected 16 kHz mono PCM16"
            raise ValueError(msg)
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
