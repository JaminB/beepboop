"""API skeleton: health, sessions, CSRF, problem+json, OpenAPI generation."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from beepboop.api import SESSION_COOKIE, ApiDeps, create_app, openapi_document, write_openapi
from beepboop.db import Database, repo
from beepboop.module import ToolContext
from beepboop.policy import ApprovalService, MemoryApprovalStore, Principal, ProfileKind
from beepboop.registry import RegisteredTool, Registry
from beepboop.testing import fake_context

TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "docs" / "module-template"


class FakeContextFactory:
    def for_call(
        self, tool: RegisteredTool, principal: Principal, *, deadline: float | None, approval: Any
    ) -> ToolContext:
        return fake_context(TEMPLATE_DIR, profile=None, in_tool_call=True, principal=principal)


PUBLIC = "https://beepboop.local"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, Database]]:
    db = await Database.open(tmp_path / "db.db", batch_window_s=0.0)
    state = await db.read(lambda c: repo.policy_view.load_policy_state(c, tz=UTC))
    registry = Registry(
        lambda: state, ApprovalService(MemoryApprovalStore(), lambda: "A"), FakeContextFactory()
    )
    app = create_app(
        ApiDeps(
            db=db, registry=registry, policy_state=lambda: state, public_url=PUBLIC, version="t"
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=PUBLIC) as http:
        yield http, db
    await db.close()


async def _login(db: Database, kind: ProfileKind = "admin") -> str:
    profile = await db.write(
        lambda c: repo.profiles.insert(c, f"P-{kind}", kind, "critical"), durable=True
    )
    token = f"tok-{kind}"
    await db.write(
        lambda c: repo.sessions.create(c, repo.sessions.hash_token(token), profile.id), durable=True
    )
    return token


async def test_health_is_public_and_detail_needs_admin(
    client: tuple[AsyncClient, Database],
) -> None:
    http, db = client
    r = await http.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["schema_version"] >= 1
    r = await http.get("/api/health/detail")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    member = await _login(db, "member")
    http.cookies.set(SESSION_COOKIE, member)
    r = await http.get("/api/health/detail")
    assert r.status_code == 403
    admin = await _login(db, "admin")
    http.cookies.set(SESSION_COOKIE, admin)
    r = await http.get("/api/health/detail")
    assert r.status_code == 200
    assert r.json()["tools"] == 0


async def test_me_and_scopes(client: tuple[AsyncClient, Database]) -> None:
    http, db = client
    token = await _login(db, "member")
    http.cookies.set(SESSION_COOKIE, token)
    r = await http.get("/api/me")
    assert r.status_code == 200
    assert r.json()["scopes"] == ["self"]
    http.cookies.set(SESSION_COOKIE, "garbage")
    r = await http.get("/api/me")
    assert r.status_code == 401


async def test_csrf_blocks_cross_site_writes(client: tuple[AsyncClient, Database]) -> None:
    http, _ = client
    r = await http.post("/api/health", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = await http.post(
        "/api/health", headers={"Sec-Fetch-Site": "same-origin", "Origin": "https://evil"}
    )
    assert r.status_code == 403
    r = await http.post("/api/health", headers={"Sec-Fetch-Site": "same-origin", "Origin": PUBLIC})
    assert r.status_code == 405  # passed CSRF; no POST route


def test_openapi_document_is_deterministic(tmp_path: Path) -> None:
    doc = openapi_document("1.2.3")
    assert doc["info"]["version"] == "1.2.3"
    assert "/api/health" in doc["paths"]
    assert "/api/me" in doc["paths"]
    ops = {op["operationId"] for p in doc["paths"].values() for op in p.values()}
    assert {"health_status", "health_detail", "me_read"} <= ops
    out = write_openapi(tmp_path / "openapi.json", "1.2.3")
    assert json.loads(out.read_text(encoding="utf-8")) == doc
