"""``GET /api/health`` (docs/OPERATIONS.md §11): bare ``ok`` without auth; details need an admin.

The detail body is the OPERATIONS §11 ``HealthReport`` (``beepboop.health.collect_health``)
plus the loaded modules and tool count; an unknown value is ``null``, never a guess.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from beepboop.api.deps import Admin, Deps
from beepboop.health import HealthReport, collect_health

router = APIRouter(prefix="/api/health", tags=["health"])


class HealthStatus(BaseModel):
    status: str
    version: str
    schema_version: int


class HealthDetail(HealthReport):
    modules: list[str]
    tools: int


@router.get("", operation_id="health_status")
async def health_status(deps: Deps) -> HealthStatus:
    return HealthStatus(
        status="ok", version=deps.version, schema_version=await deps.db.schema_version()
    )


@router.get("/detail", operation_id="health_detail")
async def health_detail(deps: Deps, _admin: Admin) -> HealthDetail:
    report = await collect_health(
        deps.db,
        version=deps.version,
        now=deps.clock(),
        host_json=deps.host_json_path(),
        ca_cert=deps.ca_cert,
        probes=deps.health_probes,
    )
    return HealthDetail(
        **report.model_dump(),
        modules=[m.id for m, _ in deps.registry.modules()],
        tools=len(deps.registry.tools()),
    )
