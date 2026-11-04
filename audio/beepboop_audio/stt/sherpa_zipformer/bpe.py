"""Hotword phrases -> BPE pieces, from the zipformer's pinned ``bpe.model`` (VOICE_PIPELINE §10.2).

sherpa-onnx biases a transducer towards a hotword by matching its *token*
sequence. The pinned streaming zipformer export ships only ``tokens.txt``; the
SentencePiece model it was trained with (icefall LibriSpeech ``lang_bpe_500``,
a unigram model) is pinned separately in ``models/manifest.yaml``. This module
reads that file directly (a SentencePiece ``ModelProto`` is a small protobuf:
pieces with their log-probabilities), so there is no ``sentencepiece``
dependency:

* :func:`vocab_text` renders the ``bpe.vocab`` text (``piece<TAB>score`` per
  line) that sherpa-onnx's ``modeling_unit="bpe"`` needs. The provider writes it
  to a temporary file only for the duration of the recognizer's construction.
* :class:`BpeEncoder` runs the same unigram Viterbi search to decide whether a
  phrase is encodable at all; phrases with characters outside the vocabulary
  (digits, symbols) are dropped before they reach sherpa-onnx.

Hotword phrases are passed to sherpa-onnx upper-cased, the case the LibriSpeech
vocabulary uses (lower-case text segments into nothing useful).
"""

from __future__ import annotations

import math
import struct
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

WORD_BOUNDARY = "▁"  # SentencePiece's "▁"

#: SentencePiece piece types (``ModelProto.SentencePiece.Type``).
_NORMAL = 1
_USER_DEFINED = 4


class BpeModelError(ValueError):
    """The file is not a SentencePiece model this encoder understands."""


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise BpeModelError("truncated varint")
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, i


def _fields(buf: bytes) -> Iterator[tuple[int, int | bytes]]:
    """Yield ``(field_number, value)`` for one protobuf message."""
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
            yield number, value
        elif wire == 2:
            length, i = _varint(buf, i)
            yield number, buf[i : i + length]
            i += length
        elif wire == 5:
            yield number, buf[i : i + 4]
            i += 4
        elif wire == 1:
            yield number, buf[i : i + 8]
            i += 8
        else:
            raise BpeModelError(f"unsupported protobuf wire type {wire}")


@dataclass(frozen=True, slots=True)
class Piece:
    text: str
    score: float
    kind: int


def read_pieces(data: bytes) -> list[Piece]:
    """The ``pieces`` of a SentencePiece ``ModelProto`` (field 1), in id order."""
    pieces: list[Piece] = []
    for number, value in _fields(data):
        if number != 1 or not isinstance(value, bytes):
            continue
        text = ""
        score = 0.0
        kind = _NORMAL
        for sub_number, sub_value in _fields(value):
            if sub_number == 1 and isinstance(sub_value, bytes):
                text = sub_value.decode("utf-8")
            elif sub_number == 2 and isinstance(sub_value, bytes):
                score = float(struct.unpack("<f", sub_value)[0])
            elif sub_number == 3 and isinstance(sub_value, int):
                kind = sub_value
        pieces.append(Piece(text, score, kind))
    if not pieces:
        raise BpeModelError("no pieces: not a SentencePiece model")
    return pieces


class BpeEncoder:
    """Unigram Viterbi segmentation over the model's normal pieces."""

    def __init__(self, pieces: list[Piece], *, uppercase: bool = True) -> None:
        self._scores: dict[str, float] = {
            p.text: p.score for p in pieces if p.kind in (_NORMAL, _USER_DEFINED) and p.text
        }
        self._max_len = max(len(t) for t in self._scores)
        self._uppercase = uppercase
        self.vocabulary = frozenset(self._scores)

    @classmethod
    def from_file(cls, path: Path) -> BpeEncoder:
        return cls(read_pieces(path.read_bytes()))

    def normalise(self, phrase: str) -> list[str]:
        text = unicodedata.normalize("NFKC", phrase)
        text = text.upper() if self._uppercase else text
        return [w for w in text.split() if w]

    def encode_word(self, word: str) -> list[str] | None:
        """Best segmentation of ``▁word``; None if some character has no piece."""
        text = WORD_BOUNDARY + word
        n = len(text)
        best: list[float] = [-math.inf] * (n + 1)
        back: list[int] = [-1] * (n + 1)
        best[0] = 0.0
        for end in range(1, n + 1):
            for start in range(max(0, end - self._max_len), end):
                if best[start] == -math.inf:
                    continue
                score = self._scores.get(text[start:end])
                if score is None:
                    continue
                candidate = best[start] + score
                if candidate > best[end]:
                    best[end] = candidate
                    back[end] = start
        if best[n] == -math.inf:
            return None
        out: list[str] = []
        end = n
        while end > 0:
            start = back[end]
            out.append(text[start:end])
            end = start
        out.reverse()
        return out

    def encode(self, phrase: str) -> list[str] | None:
        """Pieces for a whole phrase, or None if any word cannot be encoded."""
        pieces: list[str] = []
        for word in self.normalise(phrase):
            encoded = self.encode_word(word)
            if encoded is None:
                return None
            pieces.extend(encoded)
        return pieces or None


def vocab_text(pieces: list[Piece]) -> str:
    """The SentencePiece ``.vocab`` text for ``bpe_vocab``: ``piece<TAB>score`` per line."""
    return "".join(f"{p.text}\t{p.score}\n" for p in pieces)


def hotwords_argument(encoder: BpeEncoder, phrases: tuple[str, ...]) -> str:
    """The per-stream hotwords string for ``OnlineRecognizer.create_stream``.

    One normalised, upper-cased phrase per line (``modeling_unit="bpe"``).
    Phrases the vocabulary cannot encode are dropped.
    """
    lines: list[str] = []
    for phrase in phrases:
        if encoder.encode(phrase) is not None:
            lines.append(" ".join(encoder.normalise(phrase)))
    return "\n".join(dict.fromkeys(lines))


__all__ = [
    "WORD_BOUNDARY",
    "BpeEncoder",
    "BpeModelError",
    "Piece",
    "hotwords_argument",
    "read_pieces",
    "vocab_text",
]
