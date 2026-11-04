"""Build the in-memory ``PolicyState`` from the database (IDENTITY_AND_TRUST §7).

Rebuilt on any ``profiles`` / ``grants`` / ``group_members`` / ``ha_entities`` /
``voiceprints`` / ``logins`` write; ``authorize`` never reads SQL.
"""

from __future__ import annotations

import sqlite3
from datetime import tzinfo
from typing import cast

from beepboop.db.ids import now_ms
from beepboop.db.repo import grants as grants_repo
from beepboop.db.repo import ha_entities, household, logins, profiles
from beepboop.db.repo._rows import json_obj
from beepboop.policy.authorize import PolicyState
from beepboop.policy.grants import GrantIndex
from beepboop.policy.ratelimit import RateLimiter
from beepboop.policy.types import DEFAULT_CALIBRATION, Calibration, EntityInfo, ProfileView


def _numbers(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    items = cast(dict[object, object], raw)
    return {str(k): float(v) for k, v in items.items() if isinstance(v, int | float)}


VOICE_KEYS = ("tau_act", "margin_act", "tau_personal", "margin_personal")


def household_calibration(conn: sqlite3.Connection) -> Calibration:
    """System > Voice household thresholds (``household.voice.*``), VOICE_PIPELINE §8.4
    defaults where unset. The default for every profile without its own calibration."""
    values: dict[str, float] = {}
    for key in VOICE_KEYS:
        raw = household.get(conn, f"voice.{key}")
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            values[key] = float(raw)
    return Calibration(
        tau_act=values.get("tau_act", DEFAULT_CALIBRATION.tau_act),
        margin_act=values.get("margin_act", DEFAULT_CALIBRATION.margin_act),
        tau_personal=values.get("tau_personal", DEFAULT_CALIBRATION.tau_personal),
        margin_personal=values.get("margin_personal", DEFAULT_CALIBRATION.margin_personal),
    )


def load_calibrations(conn: sqlite3.Connection) -> dict[str, Calibration]:
    """Per-profile thresholds from ``voiceprints.calib_json`` (VOICE_PIPELINE §8.4 defaults,
    overridden by the household's System > Voice values)."""
    base = household_calibration(conn)
    out: dict[str, Calibration] = {}
    for row in conn.execute("SELECT profile_id, calib_json FROM voiceprints"):
        data = json_obj(str(row["calib_json"]))
        tau = _numbers(data.get("tau"))
        margin = _numbers(data.get("margin"))
        out[str(row["profile_id"])] = Calibration(
            tau_act=tau.get("act", base.tau_act),
            margin_act=margin.get("act", base.margin_act),
            tau_personal=tau.get("personal", base.tau_personal),
            margin_personal=margin.get("personal", base.margin_personal),
        )
    return out


def load_profiles(conn: sqlite3.Connection, now: int | None = None) -> dict[str, ProfileView]:
    now = now or now_ms()
    with_login = logins.profiles_with_login(conn)
    calibrations = load_calibrations(conn)
    base = household_calibration(conn)
    out: dict[str, ProfileView] = {}
    for row in profiles.list_(conn):
        out[row.id] = ProfileView(
            id=row.id,
            kind=row.kind,
            risk_ceiling=row.risk_ceiling,
            group_ids=profiles.groups_for(conn, row.id),
            disabled=row.disabled_at is not None,
            has_login=row.id in with_login,
            pin_enabled=row.pin_hash_enc is not None,
            pin_locked=row.pin_locked_until is not None and row.pin_locked_until > now,
            calibration=calibrations.get(row.id, base),
        )
    return out


def load_grants(conn: sqlite3.Connection) -> GrantIndex:
    return GrantIndex.build(grants_repo.list_all(conn))


def load_entities(conn: sqlite3.Connection) -> dict[str, EntityInfo]:
    return {e.uuid: e.to_policy() for e in ha_entities.list_(conn)}


def load_policy_state(
    conn: sqlite3.Connection, *, tz: tzinfo, rate_limiter: RateLimiter | None = None
) -> PolicyState:
    """One read transaction → a fresh snapshot. Pass the previous ``rate_limiter`` to keep
    counts.
    """
    ask = household.get(conn, "voice.ask_when_unsure")
    return PolicyState(
        profiles=load_profiles(conn),
        grants=load_grants(conn),
        entities=load_entities(conn),
        rate_limiter=rate_limiter,
        tz=tz,
        ask_when_unsure=True if ask is None else bool(ask),
    )
