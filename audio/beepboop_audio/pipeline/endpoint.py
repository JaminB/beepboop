"""Endpointing (VOICE_PIPELINE §7.2, §7.3): frame-driven, pure, no I/O.

Fed one VAD flag per 10 ms frame with its capture stamp, :class:`Endpointer`
reports the moments the pipeline acts on:

* ``speech_start`` once ``min_speech_ms`` of speech has accumulated (shorter
  blips, a cough, are ignored); ``t_speech_start`` is the first frame of it.
* ``speculate`` after ``speculative_silence_ms`` of silence following speech:
  start the final speaker embedding (§7.3). ``resumed`` if speech returns
  before the hangover expires: discard it.
* ``eos`` after the hangover (``adaptive_hangover_ms`` when the last partial
  ends in a function word or a number); ``t_eos = t_last_speech + hangover``.
* ``timeout`` when no speech started within ``speech_start_timeout_ms``.
* ``max_len`` at ``max_utterance_ms`` of speech.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from importlib import resources

import yaml

from beepboop_audio.config import EndpointConfig, as_list, as_mapping
from beepboop_audio.frames import FRAME_NS


class EndpointEvent(StrEnum):
    SPEECH_START = "speech_start"
    SPECULATE = "speculate"
    RESUMED = "resumed"
    EOS = "eos"
    TIMEOUT = "timeout"
    MAX_LEN = "max_len"


_NUMBER = re.compile(
    r"(?:\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|fifteen|twenty|thirty|forty|fifty|sixty|hundred)$"
)


@cache
def function_words(locale: str) -> frozenset[str]:
    """``pipeline/endpoint/<lang>.yaml``; unknown languages fall back to English."""
    lang = locale.split("-", 1)[0].lower()
    base = resources.files("beepboop_audio.pipeline") / "endpoint"
    path = base / f"{lang}.yaml"
    if not path.is_file():
        path = base / "en.yaml"
    loaded = as_mapping(yaml.safe_load(path.read_text(encoding="utf-8")))
    return frozenset(str(w).lower() for w in as_list(loaded.get("function_words")))


def wants_adaptive(partial: str, words: frozenset[str]) -> bool:
    """True when the last partial ends in a function word or a number."""
    tokens = partial.lower().split()
    if not tokens:
        return False
    last = tokens[-1].strip(".,?!")
    return last in words or bool(_NUMBER.search(last))


@dataclass
class _State:
    speech_start_ns: int | None = None
    candidate_start_ns: int | None = None
    candidate_speech_ns: int = 0
    last_speech_ns: int | None = None
    speech_ns: int = 0
    speculated: bool = False
    done: bool = False


class Endpointer:
    def __init__(
        self,
        config: EndpointConfig,
        t_start_ns: int,
        *,
        locale: str = "en-US",
        speech_start_timeout_ms: int | None = None,
    ) -> None:
        self.config = config
        self.t_start_ns = t_start_ns
        timeout = (
            config.speech_start_timeout_ms
            if speech_start_timeout_ms is None
            else speech_start_timeout_ms
        )
        self._timeout_ns = timeout * 1_000_000
        self._words = function_words(locale)
        self._partial = ""
        self._s = _State()
        self.t_eos_ns: int | None = None

    @property
    def speech_started(self) -> bool:
        return self._s.speech_start_ns is not None

    @property
    def t_speech_start_ns(self) -> int | None:
        return self._s.speech_start_ns

    @property
    def last_speech_ns(self) -> int | None:
        return self._s.last_speech_ns

    @property
    def speech_ms(self) -> int:
        return self._s.speech_ns // 1_000_000

    @property
    def done(self) -> bool:
        return self._s.done

    def set_partial(self, text: str) -> None:
        self._partial = text

    @property
    def hangover_ms(self) -> int:
        if wants_adaptive(self._partial, self._words):
            return self.config.adaptive_hangover_ms
        return self.config.hangover_ms

    def update(self, speech: bool, t_ns: int) -> list[EndpointEvent]:
        s = self._s
        if s.done:
            return []
        out: list[EndpointEvent] = []
        min_speech_ns = self.config.min_speech_ms * 1_000_000
        if s.speech_start_ns is None:
            if speech:
                if s.candidate_start_ns is None:
                    s.candidate_start_ns = t_ns
                s.candidate_speech_ns += FRAME_NS
                s.last_speech_ns = t_ns
                if s.candidate_speech_ns >= min_speech_ns:
                    s.speech_start_ns = s.candidate_start_ns
                    s.speech_ns = s.candidate_speech_ns
                    out.append(EndpointEvent.SPEECH_START)
            elif (
                s.candidate_start_ns is not None
                and s.last_speech_ns is not None
                and t_ns - s.last_speech_ns >= self.config.hangover_ms * 1_000_000
            ):
                s.candidate_start_ns = None  # a blip: forget it
                s.candidate_speech_ns = 0
            if s.speech_start_ns is None and t_ns - self.t_start_ns >= self._timeout_ns:
                s.done = True
                out.append(EndpointEvent.TIMEOUT)
            return out
        if speech:
            if s.speculated:
                s.speculated = False
                out.append(EndpointEvent.RESUMED)
            s.last_speech_ns = t_ns
            s.speech_ns += FRAME_NS
            if s.speech_ns >= self.config.max_utterance_ms * 1_000_000:
                s.done = True
                self.t_eos_ns = t_ns
                out.append(EndpointEvent.MAX_LEN)
            return out
        last = s.last_speech_ns if s.last_speech_ns is not None else t_ns
        silence_ns = t_ns - last
        if not s.speculated and silence_ns >= self.config.speculative_silence_ms * 1_000_000:
            s.speculated = True
            out.append(EndpointEvent.SPECULATE)
        hangover_ns = self.hangover_ms * 1_000_000
        if silence_ns >= hangover_ns:
            s.done = True
            self.t_eos_ns = last + hangover_ns
            out.append(EndpointEvent.EOS)
        return out


__all__ = ["EndpointEvent", "Endpointer", "function_words", "wants_adaptive"]
