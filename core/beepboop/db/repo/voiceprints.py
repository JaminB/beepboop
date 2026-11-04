"""``voiceprints`` and ``voice_samples`` (DATA_AND_PRIVACY §6.1, §9.1). Durable.

Centroids and embeddings arrive here already envelope-encrypted (``beepboop.secrets``);
this repo never sees a plaintext vector, and there is no audio anywhere (ADR 0018).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from beepboop.db.ids import new_ulid, now_ms
from beepboop.db.repo._rows import dump_json, json_obj, opt_str

SampleKind = Literal["enroll", "adapt", "negative"]
MAX_ADAPT_SAMPLES = 50
DEFAULT_CALIB: dict[str, object] = {
    "tau": {"act": 0.70, "personal": 0.90},
    "margin": {"act": 0.10, "personal": 0.20},
    "asnorm": {},
}


@dataclass(frozen=True, slots=True)
class VoiceprintRow:
    profile_id: str
    model_id: str
    model_sha: str
    dim: int
    centroids_enc: bytes
    k: int
    n_clips: int
    calib: dict[str, object]
    consent_at: int
    consent_version: str
    consented_by: str | None = None
    updated_at: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> VoiceprintRow:
        return cls(
            profile_id=str(row["profile_id"]),
            model_id=str(row["model_id"]),
            model_sha=str(row["model_sha"]),
            dim=int(row["dim"]),
            centroids_enc=bytes(row["centroids_enc"]),
            k=int(row["k"]),
            n_clips=int(row["n_clips"]),
            calib=json_obj(str(row["calib_json"])),
            consent_at=int(row["consent_at"]),
            consent_version=str(row["consent_version"]),
            consented_by=opt_str(row, "consented_by"),
            updated_at=int(row["updated_at"]),
        )


def upsert(conn: sqlite3.Connection, v: VoiceprintRow) -> None:
    if v.profile_id == "guest":
        msg = "guest never has a voiceprint (DATA_AND_PRIVACY §8)"
        raise ValueError(msg)
    conn.execute(
        "INSERT INTO voiceprints (profile_id, model_id, model_sha, dim, centroids_enc, k, n_clips,"
        " calib_json, consent_at, consent_version, consented_by, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(profile_id, model_id) DO UPDATE SET model_sha = excluded.model_sha,"
        " dim = excluded.dim, centroids_enc = excluded.centroids_enc, k = excluded.k,"
        " n_clips = excluded.n_clips, calib_json = excluded.calib_json, updated_at ="
        " excluded.updated_at",
        (
            v.profile_id,
            v.model_id,
            v.model_sha,
            v.dim,
            v.centroids_enc,
            v.k,
            v.n_clips,
            dump_json(v.calib),
            v.consent_at,
            v.consent_version,
            v.consented_by,
            v.updated_at or now_ms(),
        ),
    )


def get(conn: sqlite3.Connection, profile_id: str, model_id: str) -> VoiceprintRow | None:
    row = conn.execute(
        "SELECT * FROM voiceprints WHERE profile_id = ? AND model_id = ?", (profile_id, model_id)
    ).fetchone()
    return None if row is None else VoiceprintRow.from_row(row)


def list_for_model(conn: sqlite3.Connection, model_id: str) -> list[VoiceprintRow]:
    rows = conn.execute(
        "SELECT * FROM voiceprints WHERE model_id = ? ORDER BY profile_id", (model_id,)
    ).fetchall()
    return [VoiceprintRow.from_row(r) for r in rows]


def delete(conn: sqlite3.Connection, profile_id: str) -> int:
    """Consent withdrawal (§9.1): the voiceprint and every sample go."""
    conn.execute("DELETE FROM voice_samples WHERE profile_id = ?", (profile_id,))
    return conn.execute("DELETE FROM voiceprints WHERE profile_id = ?", (profile_id,)).rowcount


# ------------------------------------------------------------------ samples --


def insert_sample(
    conn: sqlite3.Connection,
    profile_id: str,
    model_id: str,
    kind: SampleKind,
    emb_enc: bytes,
    *,
    quality: dict[str, object] | None = None,
    source_turn: str | None = None,
    sample_id: str | None = None,
) -> str:
    """Insert one embedding; ``adapt`` rows are capped at 50 per profile, oldest evicted.

    ``sample_id`` lets the caller seal ``emb_enc`` with the row id in its AAD first.
    """
    if profile_id == "guest":
        msg = "guest never has voice samples (DATA_AND_PRIVACY §8)"
        raise ValueError(msg)
    sample_id = sample_id or new_ulid()
    conn.execute(
        "INSERT INTO voice_samples (id, profile_id, model_id, kind, emb_enc, quality_json,"
        " source_turn, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sample_id,
            profile_id,
            model_id,
            kind,
            emb_enc,
            None if quality is None else dump_json(quality),
            source_turn,
            now_ms(),
        ),
    )
    if kind == "adapt":
        conn.execute(
            "DELETE FROM voice_samples WHERE id IN (SELECT id FROM voice_samples"
            " WHERE profile_id = ? AND kind = 'adapt' ORDER BY created_at DESC, id DESC"
            " LIMIT -1 OFFSET ?)",
            (profile_id, MAX_ADAPT_SAMPLES),
        )
    return sample_id


def count_samples(conn: sqlite3.Connection, profile_id: str, kind: SampleKind) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM voice_samples WHERE profile_id = ? AND kind = ?",
        (profile_id, kind),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def samples(conn: sqlite3.Connection, profile_id: str, kind: SampleKind) -> list[bytes]:
    rows = conn.execute(
        "SELECT emb_enc FROM voice_samples WHERE profile_id = ? AND kind = ? ORDER BY created_at",
        (profile_id, kind),
    ).fetchall()
    return [bytes(r["emb_enc"]) for r in rows]


def sample_kind(value: str) -> SampleKind:
    if value == "enroll" or value == "adapt" or value == "negative":
        return value
    msg = f"unknown sample kind {value!r}"
    raise ValueError(msg)
