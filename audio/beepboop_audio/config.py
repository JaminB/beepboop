"""The audio service's configuration: ``audio:`` and ``providers:`` of ``/config/beepboop.yaml``.

ARCHITECTURE §9 owns the configuration model and VOICE_PIPELINE §17.1 the
audio keys. The file holds hardware and provider ids only; anything editable in
the web UI lives in core's database and reaches audio as events
(``profiles.sync``, ``mute``). Every key is optional. Environment overrides:
``BB_CONFIG`` (file path), ``BB_ALSA_DEVICE`` (the one ALSA env var),
``BB_LOCALE`` (household default locale), ``BB_MODELS_DIR`` (models root,
``/models`` in the container).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_PATH = Path("/config/beepboop.yaml")
DEFAULT_MODELS_DIR = Path("/models")


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class DeviceConfig(_Model):
    id: str = "beepboop"
    name: str = "beepboop"


class Threads(_Model):
    capture: int = Field(default=1, ge=1)
    stt: int = Field(default=2, ge=1)
    speaker: int = Field(default=2, ge=1)
    tts: int = Field(default=2, ge=1)


class EndpointConfig(_Model):
    """VOICE_PIPELINE §7.2 (all targets until M0's ``bench endpoint`` sweep)."""

    hangover_ms: int = Field(default=250, ge=0)
    adaptive_hangover_ms: int = Field(default=400, ge=0)
    speech_start_timeout_ms: int = Field(default=5000, ge=0)
    max_utterance_ms: int = Field(default=15000, ge=1000)
    min_speech_ms: int = Field(default=300, ge=0)
    speculative_silence_ms: int = Field(default=100, ge=0)


class AudioSection(_Model):
    alsa_device: str = "default"
    firmware: str = "usb-16k-6ch"
    capture_channel: int = Field(default=0, ge=0)
    capture_channels: int = Field(default=6, ge=1)
    playback_channels: int = Field(default=2, ge=1)
    threads: Threads = Threads()
    endpoint: EndpointConfig = EndpointConfig()
    reply_window_ms: int = Field(default=8000, ge=0)
    barge_in: bool = False  # v0.3; never enabled in v0.1


class WakeModelConfig(_Model):
    id: str
    path: str | None = None
    probability_cutoff: float = Field(default=0.97, gt=0.0, le=1.0)
    sliding_window_size: int = Field(default=5, ge=1)
    refractory_ms: int = Field(default=2000, ge=0)


class WakeProviderConfig(_Model):
    id: str = "microwakeword"
    models: list[WakeModelConfig | str] = Field(default_factory=lambda: ["hey_beepboop"])
    vad_gate: bool = True

    def model_configs(self) -> list[WakeModelConfig]:
        return [WakeModelConfig(id=m) if isinstance(m, str) else m for m in self.models]


class SpeakerProviderConfig(_Model):
    id: str = "sherpa-campp"


class SttProviderConfig(_Model):
    id: str = "sherpa-zipformer"
    hotwords_score: float = Field(default=1.5, ge=0.0)
    max_active_paths: int = Field(default=4, ge=1)


class TtsProviderConfig(_Model):
    id: str = "sherpa-piper"
    default_voice: str = "en_US-lessac-low"


class ProvidersSection(_Model):
    wake: WakeProviderConfig = WakeProviderConfig()
    speaker: SpeakerProviderConfig = SpeakerProviderConfig()
    stt: SttProviderConfig = SttProviderConfig()
    tts: TtsProviderConfig = TtsProviderConfig()


class AudioConfig(_Model):
    device: DeviceConfig = DeviceConfig()
    locale: str = "en-US"
    audio: AudioSection = AudioSection()
    providers: ProvidersSection = ProvidersSection()
    models_dir: Path = DEFAULT_MODELS_DIR


def as_mapping(value: object) -> dict[str, object]:
    """A YAML mapping with string keys, or ``{}`` (YAML loads untyped)."""
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast("dict[object, object]", value).items()}


def as_list(value: object) -> list[object]:
    return list(cast("list[object]", value)) if isinstance(value, list) else []


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> AudioConfig:
    """Read the household file (missing file = all defaults) and apply ``BB_*`` overrides."""
    environ = os.environ if env is None else env
    file_path = path or Path(environ.get("BB_CONFIG", str(DEFAULT_CONFIG_PATH)))
    raw: dict[str, object] = {}
    if file_path.is_file():
        raw = as_mapping(yaml.safe_load(file_path.read_text(encoding="utf-8")))
    # Audio reads only its own sections plus the shared device and locale keys.
    wanted = {k: raw[k] for k in ("device", "locale", "audio", "providers") if k in raw}
    config = AudioConfig.model_validate(wanted)
    audio = config.audio
    if alsa := environ.get("BB_ALSA_DEVICE"):
        audio = audio.model_copy(update={"alsa_device": alsa})
    locale = environ.get("BB_LOCALE") or config.locale
    models_dir = Path(environ.get("BB_MODELS_DIR", str(DEFAULT_MODELS_DIR)))
    return config.model_copy(update={"audio": audio, "locale": locale, "models_dir": models_dir})


Backend = Literal["alsa", "file", "null"]

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_MODELS_DIR",
    "AudioConfig",
    "AudioSection",
    "Backend",
    "DeviceConfig",
    "EndpointConfig",
    "ProvidersSection",
    "SpeakerProviderConfig",
    "SttProviderConfig",
    "Threads",
    "TtsProviderConfig",
    "WakeModelConfig",
    "WakeProviderConfig",
    "as_list",
    "as_mapping",
    "load_config",
]
