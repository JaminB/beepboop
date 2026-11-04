"""Repositories over a real migrated file, including retention and the §9.3 cascade."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from beepboop.db import Database, repo, retention, to_ms
from beepboop.db.repo.approvals import DbApprovalStore
from beepboop.db.repo.kv import KVLimitError
from beepboop.db.repo.turns import ToolCallRow, TurnRow
from beepboop.policy import (
    ALL_SCOPES,
    ApprovalService,
    Grant,
    Principal,
    SimpleTool,
    apply_preset,
    authorize,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _seed(conn: sqlite3.Connection) -> tuple[str, str]:
    alex = repo.profiles.insert(conn, "Alex", "admin", "critical")
    mia = repo.profiles.insert(conn, "Mia", "child", "act")
    counter = iter(range(100))
    for pid, preset in ((alex.id, "admin"), (mia.id, "child")):
        rows = apply_preset(preset, pid, lambda: f"g{next(counter)}")
        for g in rows.group_ids:
            repo.profiles.add_to_group(conn, pid, g)
        repo.grants.insert_many(conn, rows.grants)
    repo.logins.insert(conn, alex.id, "alex", "$argon2id$fake")
    return alex.id, mia.id


async def test_profiles_presets_and_policy_view(db: Database) -> None:
    alex, mia = await db.write(_seed, durable=True)
    state = await db.read(lambda c: repo.policy_view.load_policy_state(c, tz=UTC))
    assert state.profiles[alex].has_login
    assert state.profiles[alex].group_ids == {
        "household",
        "adults",
    }
    assert state.profiles[mia].risk_ceiling == "act"
    assert "guest" in state.profiles
    p = Principal(mia, "voice", 0.9, 0.3, "main", ALL_SCOPES, False, "T1")
    assert (
        authorize(state, p, SimpleTool("caldav.events", "personal"), {}, [], NOW).reason
        == "ceiling"
    )
    assert authorize(state, p, SimpleTool("timer.set", "act"), {}, [], NOW).outcome == "allow"
    with pytest.raises(repo.profiles.ProfileExistsError):
        await db.write(
            lambda c: repo.profiles.insert(c, "Alex", "member", "critical"), durable=True
        )


async def test_grants_upsert_on_unique_key_and_expiry_purge(db: Database) -> None:
    g = Grant("g1", "profile", "guest", "tool_glob", "home.*", "act", source="admin")
    await db.write(lambda c: repo.grants.insert(c, g), durable=True)
    g2 = Grant("g2", "profile", "guest", "tool_glob", "home.*", "read", source="admin")
    await db.write(lambda c: repo.grants.insert(c, g2), durable=True)
    rows = await db.read(repo.grants.list_all)
    assert [(r.id, r.max_risk) for r in rows if r.source == "admin"] == [("g2", "read")]
    from beepboop.policy import Schedule

    expired = Grant(
        "g3", "profile", "guest", "tool_glob", "x.*", "act",
        schedule=Schedule(expires_at=NOW - timedelta(hours=1)), source="admin",
    )  # fmt: skip
    await db.write(lambda c: repo.grants.insert(c, expired), durable=True)
    assert (
        await db.write(lambda c: repo.grants.purge_expired(c, NOW.isoformat()), durable=True) == 1
    )


async def test_ha_entities_inbox_and_removal(db: Database) -> None:
    from beepboop.db.repo.ha_entities import EntityRow

    row = EntityRow("u1", "light.kitchen", "light", area_id="kitchen", aliases=("the lamp",))
    await db.write(lambda c: repo.ha_entities.upsert(c, row), durable=True)
    got = await db.read(lambda c: repo.ha_entities.get(c, "u1"))
    assert got is not None
    assert got.status == "new"
    assert got.aliases == ("the lamp",)
    await db.write(lambda c: repo.ha_entities.set_status(c, "u1", "active", "act"), durable=True)
    await db.write(lambda c: repo.ha_entities.upsert(c, row), durable=True)  # refresh keeps status
    got = await db.read(lambda c: repo.ha_entities.get(c, "u1"))
    assert got is not None
    assert got.status == "active"
    assert got.declared_risk == "act"
    await db.write(lambda c: repo.ha_entities.mark_removed(c, "u1"), durable=True)
    far = to_ms(NOW + timedelta(days=400))
    assert await db.write(lambda c: repo.ha_entities.purge_removed(c, far), durable=True) == 1


async def test_kv_limits_and_scopes(db: Database) -> None:
    await db.write(lambda c: repo.kv.set_(c, "timers", None, "a", {"x": 1}))
    await db.write(lambda c: repo.kv.set_(c, "timers", "guest", "a", [1, 2]))
    assert await db.read(lambda c: repo.kv.get(c, "timers", None, "a")) == {"x": 1}
    assert await db.read(lambda c: repo.kv.get(c, "timers", "guest", "a")) == [1, 2]
    assert await db.read(lambda c: repo.kv.keys(c, "timers", None)) == ["a"]
    with pytest.raises(KVLimitError):
        await db.write(lambda c: repo.kv.set_(c, "timers", None, "big", "x" * 70_000))
    assert await db.write(lambda c: repo.kv.delete(c, "timers", None, "a"))


async def test_sessions_lifecycle(db: Database) -> None:
    alex, _ = await db.write(_seed, durable=True)
    h = repo.sessions.hash_token("tok")
    await db.write(lambda c: repo.sessions.create(c, h, alex, ua="ua"), durable=True)
    s = await db.read(lambda c: repo.sessions.get(c, h))
    assert s is not None
    assert s.is_valid(s.created_at + 1000)
    assert not s.is_valid(s.created_at + 31 * 86_400_000)
    assert await db.write(lambda c: repo.sessions.delete_for_profile(c, alex), durable=True) == 1


async def test_approvals_db_store_transitions(db: Database) -> None:
    alex, _ = await db.write(_seed, durable=True)
    svc = ApprovalService(DbApprovalStore(db), lambda: "AP1")
    p = Principal(alex, "voice", 0.9, 0.3, "main", ALL_SCOPES, False, "T1")
    rec, nonce = await svc.create(p, "home.set", {"x": 1}, ["u"], "Unlock front door", NOW)
    assert await svc.consume(rec.id, NOW) is None
    done = await svc.decide(rec.id, alex, "approved", "web", NOW, nonce)
    assert not isinstance(done, str)
    assert await svc.decide(rec.id, alex, "approved", "web", NOW, nonce) == "already_decided"
    consumed = await svc.consume(rec.id, NOW)
    assert consumed is not None
    assert consumed.executed_at is not None
    assert await svc.consume(rec.id, NOW) is None
    assert await svc.extend_once(rec.id, NOW) is False  # decided already


async def test_turns_tool_calls_explain_and_delete_turn(db: Database) -> None:
    alex, _ = await db.write(_seed, durable=True)
    turn = TurnRow(
        "T1", alex, "voice", "en-US", "llm", "sherpa-zipformer", "sherpa-piper", transcript="hi"
    )
    await db.write(lambda c: repo.turns.insert_turn(c, turn))
    plain = ToolCallRow(
        "T1", 1, "home.turn_on", "ha", {"target": "lamp"}, "act", "allow", "ok", profile_id=alex
    )
    critical = ToolCallRow(
        "T1", 2, "home.set", "ha", {"x": "[redacted]"}, "critical", "confirm", "ok", profile_id=alex
    )
    await db.write(
        lambda c: (repo.turns.insert_tool_call(c, plain), repo.turns.insert_tool_call(c, critical))
    )
    ex = await db.read(lambda c: repo.turns.explain(c, "T1"))
    assert ex is not None
    assert [c.seq for c in ex.tool_calls] == [1, 2]
    assert await db.write(lambda c: retention.delete_turn(c, "T1"))
    ex = await db.read(lambda c: repo.turns.explain(c, "T1"))
    assert ex is not None
    assert ex.turn.transcript is None
    assert [c.tool for c in ex.tool_calls] == ["home.set"]
    assert ex.tool_calls[0].profile_id is None


async def test_nightly_purge_respects_retention_and_safety_records(db: Database) -> None:
    alex, _ = await db.write(_seed, durable=True)
    old = to_ms(NOW - timedelta(days=40))
    turn = TurnRow("T1", alex, "voice", "en-US", "llm", "s", "t", transcript="old", created_at=old)
    call = ToolCallRow(
        "T1", 1, "home.set", "ha", {}, "critical", "confirm", "ok", profile_id=alex, created_at=old
    )
    fresh = TurnRow(
        "T2", alex, "voice", "en-US", "fast", "s", "t", transcript="new", created_at=to_ms(NOW)
    )
    await db.write(
        lambda c: (
            repo.turns.insert_turn(c, turn),
            repo.turns.insert_tool_call(c, call),
            repo.turns.insert_turn(c, fresh),
        )
    )
    counts = await db.write(lambda c: retention.nightly_purge(c, NOW), durable=True)
    assert counts.safety_records == 1
    kept = await db.read(lambda c: repo.turns.get_turn(c, "T1"))
    assert kept is not None
    assert kept.transcript is None
    assert kept.profile_id == "guest"
    assert await db.read(lambda c: repo.turns.get_turn(c, "T2")) is not None
    log = await db.read(lambda c: repo.audit.list_(c, limit=1))
    assert log[0].action == "retention.purge"
    assert log[0].details == counts.as_dict()


async def test_delete_profile_cascade_leaves_only_pseudonymised_safety_records(
    db: Database,
) -> None:
    alex, mia = await db.write(_seed, durable=True)
    sam = await db.write(
        lambda c: repo.profiles.insert(c, "Sam", "admin", "critical"), durable=True
    )
    turn = TurnRow("T1", mia, "voice", "en-US", "llm", "s", "t", transcript="x")
    call = ToolCallRow("T1", 1, "home.set", "ha", {}, "critical", "confirm", "ok", profile_id=mia)
    await db.write(
        lambda c: (repo.turns.insert_turn(c, turn), repo.turns.insert_tool_call(c, call))
    )
    await db.write(lambda c: repo.kv.set_(c, "lists", mia, "todo", ["a"]))
    await db.write(
        lambda c: repo.audit.record(
            c, actor_id=mia, actor_name="Mia", via="web", action="login.ok"
        ),
        durable=True,
    )
    await db.write(
        lambda c: retention.delete_profile(c, mia, acting_admin=alex, now=NOW), durable=True
    )

    def _rows(c: sqlite3.Connection) -> dict[str, int]:
        def n(sql: str) -> int:
            return int(c.execute(sql, (mia,)).fetchone()[0])

        return {
            "profiles": n("SELECT COUNT(*) FROM profiles WHERE id=?"),
            "kv": n("SELECT COUNT(*) FROM kv WHERE profile_id=?"),
            "grants": n("SELECT COUNT(*) FROM grants WHERE subject_id=?"),
            "groups": n("SELECT COUNT(*) FROM group_members WHERE profile_id=?"),
            "turns": n("SELECT COUNT(*) FROM turns WHERE profile_id=?"),
            "calls": n("SELECT COUNT(*) FROM tool_calls WHERE profile_id=?"),
            "audit": n("SELECT COUNT(*) FROM audit WHERE actor_id=?"),
            "safety": int(c.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]),
        }

    rows = await db.read(_rows)
    assert {k: v for k, v in rows.items() if k != "safety"} == dict.fromkeys(
        ("profiles", "kv", "grants", "groups", "turns", "calls", "audit"), 0
    )
    assert rows["safety"] == 1
    with pytest.raises(retention.LastAdminError):
        await db.write(
            lambda c: (
                retention.delete_profile(c, sam.id, acting_admin=None, now=NOW),
                retention.delete_profile(c, alex, acting_admin=None, now=NOW),
            ),
            durable=True,
        )
    with pytest.raises(ValueError, match="guest"):
        await db.write(
            lambda c: retention.delete_profile(c, "guest", acting_admin=None, now=NOW), durable=True
        )


async def test_voiceprints_cap_adapt_samples_and_refuse_guest(db: Database) -> None:
    alex, _ = await db.write(_seed, durable=True)
    for _ in range(55):
        await db.write(
            lambda c: repo.voiceprints.insert_sample(c, alex, "campp", "adapt", b"\x00"),
            durable=True,
        )
    assert await db.read(lambda c: repo.voiceprints.count_samples(c, alex, "adapt")) == 50
    with pytest.raises(ValueError, match="guest"):
        await db.write(
            lambda c: repo.voiceprints.insert_sample(c, "guest", "campp", "adapt", b""),
            durable=True,
        )


async def test_household_and_integrations(db: Database) -> None:
    assert await db.read(lambda c: repo.household.get(c, "transcript_retention_days")) == 30
    await db.write(lambda c: repo.household.set_(c, "transcript_retention_days", 7), durable=True)
    assert await db.read(lambda c: repo.household.get(c, "transcript_retention_days")) == 7
    row = await db.write(
        lambda c: repo.integrations.upsert(
            c, "weather", "household", None, settings={"units": "metric"}, status="ok"
        ),
        durable=True,
    )
    again = await db.write(
        lambda c: repo.integrations.upsert(c, "weather", "household", None, status="reauth"),
        durable=True,
    )
    assert again.id == row.id
    assert again.settings == {"units": "metric"}
    assert again.status == "reauth"
