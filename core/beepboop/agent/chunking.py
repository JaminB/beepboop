"""Sentence chunking and the speech post-processor (VOICE_PIPELINE §11.2, LLM_PROVIDERS §7).

Core splits the LLM text stream on sentence boundaries (``.?!;:`` followed by whitespace)
with a 120-character soft limit, and sends one ``speak`` per sentence as soon as it is
complete. The *last* flag needs to know a sentence is the final one, so the chunker holds
back exactly one complete sentence until the next one starts or the stream ends: the
first sentence leaves as soon as the second begins (one token later), and a one-sentence
reply leaves at ``Done``.

``clean_for_speech`` is the prompt-independent backstop of LLM_PROVIDERS §7: residual
markdown is stripped and whitespace collapsed before anything reaches TTS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SOFT_LIMIT = 120
_BOUNDARY = re.compile(r"[.?!;:](?=\s)")

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_CODE = re.compile(r"`+([^`]*)`+")
_MD_EMPH = re.compile(r"(\*{1,3}|_{2,3})(\S(?:.*?\S)?)\1")
_MD_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_MD_BULLET = re.compile(r"(?m)^\s*(?:[-*+•]|\d+[.)])\s+")
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿️]")
_WS = re.compile(r"\s+")


def clean_for_speech(text: str) -> str:
    """Strip markdown, URLs and emoji; collapse whitespace. Never adds words."""
    t = _MD_LINK.sub(r"\1", text)
    t = _MD_CODE.sub(r"\1", t)
    t = _MD_HEADING.sub("", t)
    t = _MD_BULLET.sub("", t)
    t = _MD_EMPH.sub(r"\2", t)
    t = _URL.sub("", t)
    t = _EMOJI.sub("", t)
    t = t.replace("*", "").replace("#", "")
    return _WS.sub(" ", t).strip()


def _split_point(buffer: str) -> int | None:
    """Index just after the first complete sentence in ``buffer``, or ``None``."""
    m = _BOUNDARY.search(buffer)
    if m is not None:
        return m.end()
    if len(buffer) > SOFT_LIMIT:
        cut = buffer.rfind(" ", 0, SOFT_LIMIT)
        return cut if cut > 0 else SOFT_LIMIT
    return None


@dataclass
class SentenceChunker:
    """Feed text deltas; get speakable sentences, one held back for the ``last`` flag."""

    _buffer: str = ""
    _held: str | None = None
    _all: list[str] = field(default_factory=list[str])

    def feed(self, delta: str) -> list[str]:
        """Sentences that are now safe to send with ``last=False``."""
        self._buffer += delta
        out: list[str] = []
        while True:
            cut = _split_point(self._buffer)
            if cut is None:
                break
            sentence = clean_for_speech(self._buffer[:cut])
            self._buffer = self._buffer[cut:]
            if not sentence:
                continue
            if self._held is not None:
                out.append(self._held)
            self._held = sentence
            self._all.append(sentence)
        if self._held is not None and self._buffer.strip():
            # the next sentence has started: the held one is not the last
            out.append(self._held)
            self._held = None
        return out

    def finish(self) -> str | None:
        """The final sentence (to send with ``last=True``), or ``None`` if nothing is left."""
        tail = clean_for_speech(self._buffer)
        self._buffer = ""
        if tail:
            self._all.append(tail)
        parts = [p for p in (self._held, tail) if p]
        self._held = None
        return " ".join(parts) if parts else None

    @property
    def text(self) -> str:
        """Everything chunked so far, as spoken."""
        return " ".join(self._all)


__all__ = ["SOFT_LIMIT", "SentenceChunker", "clean_for_speech"]
