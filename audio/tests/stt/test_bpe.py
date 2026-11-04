"""Hotword tokenisation from the pinned bpe.model (VOICE_PIPELINE §10.2)."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from beepboop_audio.models import STT_ZIPFORMER, STT_ZIPFORMER_BPE, ModelStore
from beepboop_audio.stt.sherpa_zipformer.bpe import (
    WORD_BOUNDARY,
    BpeEncoder,
    BpeModelError,
    Piece,
    hotwords_argument,
    read_pieces,
    vocab_text,
)
from beepboop_audio.stt.sherpa_zipformer.provider import words_from_tokens

MODELS = Path(__file__).resolve().parents[3] / "models"


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _piece(text: str, score: float, kind: int = 1) -> bytes:
    body = _field(1, text.encode())
    body += _varint(2 << 3 | 5) + struct.pack("<f", score)
    body += _varint(3 << 3 | 0) + _varint(kind)
    return _field(1, body)


def synthetic_model() -> bytes:
    pieces = [
        ("<unk>", 0.0, 2),
        (WORD_BOUNDARY, -2.0, 1),
        (WORD_BOUNDARY + "KITCHEN", -5.0, 1),
        (WORD_BOUNDARY + "KIT", -3.0, 1),
        ("CHEN", -3.0, 1),
        ("K", -6.0, 1),
        ("I", -6.0, 1),
        ("T", -6.0, 1),
        ("C", -6.0, 1),
        ("H", -6.0, 1),
        ("E", -6.0, 1),
        ("N", -6.0, 1),
        (WORD_BOUNDARY + "LAMP", -4.0, 1),
    ]
    blob = b"".join(_piece(t, s, k) for t, s, k in pieces)
    return blob + _field(2, _varint(3 << 3) + _varint(1))  # trainer_spec.model_type = UNIGRAM


def test_read_pieces_and_viterbi() -> None:
    pieces = read_pieces(synthetic_model())
    assert pieces[2] == Piece(WORD_BOUNDARY + "KITCHEN", -5.0, 1)
    enc = BpeEncoder(pieces)
    assert enc.encode_word("KITCHEN") == [WORD_BOUNDARY + "KITCHEN"]  # -5 beats -3 + -3
    assert enc.encode("kitchen lamp") == [WORD_BOUNDARY + "KITCHEN", WORD_BOUNDARY + "LAMP"]
    assert enc.encode("kitchen 3") is None  # "3" has no piece
    assert "<unk>" not in enc.vocabulary


def test_hotwords_argument_drops_unencodable_and_dedups() -> None:
    enc = BpeEncoder(read_pieces(synthetic_model()))
    arg = hotwords_argument(enc, ("kitchen lamp", "Kitchen  Lamp", "lamp 42"))
    assert arg == "KITCHEN LAMP"


def test_vocab_text() -> None:
    text = vocab_text([Piece("A", -1.5, 1), Piece("B", 0.0, 1)])
    assert text == "A\t-1.5\nB\t0.0\n"


def test_not_a_model() -> None:
    with pytest.raises(BpeModelError):
        read_pieces(b"")
    with pytest.raises(BpeModelError):
        read_pieces(b"\x0f")


def test_words_from_tokens() -> None:
    words = words_from_tokens(
        [WORD_BOUNDARY + "TURN", " OFF", WORD_BOUNDARY + "KIT", "CHEN"],
        [0.1, 0.3, 0.5, 0.6],
        t0_ns=1_000,
        t_end_ns=10**9,
    )
    assert [w for w, _, _ in words] == ["turn", "off", "kitchen"]
    assert words[0][1] == 1_000 + 100_000_000
    assert words[-1][2] == 10**9


@pytest.mark.skipif(
    not ModelStore(MODELS).available(STT_ZIPFORMER_BPE)
    or not ModelStore(MODELS).available(STT_ZIPFORMER),
    reason="zipformer or its bpe.model not installed (python -m beepboop_bench.models fetch)",
)
def test_pinned_bpe_model_matches_the_zipformer_tokens() -> None:
    store = ModelStore(MODELS)
    pieces = read_pieces(store.get(STT_ZIPFORMER_BPE).file("model").read_bytes())
    tokens = [
        line.split()[0]
        for line in store.get(STT_ZIPFORMER).file("tokens").read_text(encoding="utf-8").splitlines()
    ]
    assert [p.text for p in pieces] == [t for t in tokens if t != "<blk>" and t != "<sos/eos>"][
        : len(pieces)
    ] or {p.text for p in pieces if p.kind == 1} <= set(tokens)
    enc = BpeEncoder(pieces)
    assert enc.encode("kitchen lights") is not None
