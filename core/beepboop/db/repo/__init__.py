"""Repositories: plain functions over a ``sqlite3.Connection`` (DATA_AND_PRIVACY §4).

Each module covers one table family and is used as ``await db.read(lambda c: profiles.get(c, id))``
or ``await db.write(lambda c: grants.insert(c, g), durable=True)``. Durability per table
(§3): ``durable=True`` for profiles, logins, sessions, grants, approvals, secrets,
voiceprints, keyring, audit; trace writes (``durable=False``) for turns, tool_calls,
conversation, kv.
"""

from beepboop.db.repo import (
    approvals,
    audit,
    conversation,
    grants,
    ha_entities,
    household,
    integrations,
    kv,
    logins,
    policy_view,
    profiles,
    sessions,
    turns,
    voiceprints,
)

__all__ = [
    "approvals",
    "audit",
    "conversation",
    "grants",
    "ha_entities",
    "household",
    "integrations",
    "kv",
    "logins",
    "policy_view",
    "profiles",
    "sessions",
    "turns",
    "voiceprints",
]
