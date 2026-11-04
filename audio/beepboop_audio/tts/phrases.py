"""System phrases and the pre-rendered clip set (VOICE_PIPELINE §11.3, §11.6).

:data:`SYSTEM_PHRASES` is the en-US text of every §11.6 key and
:data:`ACK_CLIPS` the short acks. :class:`ClipCache` holds, per voice and
rate, the rendered PCM of every placeholder-free key plus the digits 0-9, and
an LRU of the 200 most recent synthesised sentences. The cache is in RAM
only: entries are synthesised output, never captured audio, and are rebuilt
after a restart (ADR 0018 forbids nothing here, but keeping no audio file of any
kind makes the "no audio bytes on disk" check, ROADMAP v0.1 #8, unambiguous).
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict

from beepboop_audio.types import PCM16

SYSTEM_PHRASES: dict[str, str] = {
    "claim.code": "Your setup code is {d1} {d2} {d3} {d4} {d5} {d6}.",
    "claim.fingerprint": "My certificate fingerprint starts {f1} {f2} {f3} {f4} {f5} {f6}.",
    "approval.prompt": "To {summary}, approve on your phone.",
    "approval.prompt_pin": "To {summary}, approve on your phone, or say your PIN.",
    "approval.waiting": "Still waiting for your phone.",
    "approval.done": "Done. {summary_past}.",
    "approval.denied": "Okay, I won't.",
    "approval.expired": (
        "That approval timed out, so I didn't do it. Ask again when your phone's ready."
    ),
    "approval.no_login": (
        "You haven't set up beepboop on your phone yet. Ask {admin} to send you an invite."
    ),
    "personal.unsure": (
        "That sounded like you, {name}, but I'm not sure enough for your {thing}. "
        "Approve on your phone, or say your PIN."
    ),
    "unknown_speaker": (
        "I didn't recognise your voice, so I can't do that. "
        "If you live here, try again a bit closer."
    ),
    "ceiling.child": "I can't do that for you, {name}. Ask a grown-up.",
    "not_granted": "You don't have access to the {thing}. An adult can change that in the app.",
    "pin.prompt": "Please say your PIN.",
    "pin.mismatch": "That wasn't it. Approve on your phone instead.",
    "pin.locked": "Your PIN is locked for a few minutes. Use your phone.",
    "offline": "I can't reach my brain right now, but I can still control the house.",
    "reauth": "Your {integration} needs reconnecting in the app.",
    "not_me": "Okay, noted that wasn't you.",
    "forget": "Okay, I've forgotten that.",
    "cancel": "Cancelled.",
}

ACK_CLIPS: dict[str, str] = {
    "done": "Done.",
    "ok": "OK.",
    "sure": "Sure.",
    "cancelled": "Cancelled.",
    "timer_set": "Timer set.",
}

DIGITS: tuple[str, ...] = tuple(str(d) for d in range(10))
LRU_SIZE = 200
_PLACEHOLDER = re.compile(r"\{[a-z0-9_]+\}")


def has_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER.search(text))


def prerender_keys() -> dict[str, str]:
    """Every clip key with fixed text: placeholder-free system phrases plus the acks."""
    keys = {k: v for k, v in SYSTEM_PHRASES.items() if not has_placeholder(v)}
    keys.update(ACK_CLIPS)
    return keys


def spoken_text(key: str) -> str | None:
    """The fixed text behind a spoken clip key (acks win over phrases for shared keys)."""
    return prerender_keys().get(key)


type VoiceKey = tuple[str, float]


class ClipCache:
    """Rendered clips and recent sentences, per ``(voice_id, rate)``. Thread-safe."""

    def __init__(self, lru_size: int = LRU_SIZE) -> None:
        self._clips: dict[VoiceKey, dict[str, PCM16]] = {}
        self._lru: dict[VoiceKey, OrderedDict[str, PCM16]] = {}
        self._lru_size = lru_size
        self._lock = threading.Lock()

    @staticmethod
    def voice_key(voice_id: str, rate: float) -> VoiceKey:
        return (voice_id, round(rate, 2))

    def put_clip(self, voice: VoiceKey, key: str, pcm: PCM16) -> None:
        with self._lock:
            self._clips.setdefault(voice, {})[key] = pcm

    def clip(self, voice: VoiceKey, key: str) -> PCM16 | None:
        with self._lock:
            return self._clips.get(voice, {}).get(key)

    def has_voice(self, voice: VoiceKey) -> bool:
        with self._lock:
            return voice in self._clips

    def put_sentence(self, voice: VoiceKey, text: str, pcm: PCM16) -> None:
        with self._lock:
            lru = self._lru.setdefault(voice, OrderedDict())
            lru[text] = pcm
            lru.move_to_end(text)
            while len(lru) > self._lru_size:
                lru.popitem(last=False)

    def sentence(self, voice: VoiceKey, text: str) -> PCM16 | None:
        with self._lock:
            lru = self._lru.get(voice)
            if lru is None or text not in lru:
                return None
            lru.move_to_end(text)
            return lru[text]


__all__ = [
    "ACK_CLIPS",
    "DIGITS",
    "LRU_SIZE",
    "SYSTEM_PHRASES",
    "ClipCache",
    "has_placeholder",
    "prerender_keys",
    "spoken_text",
]
