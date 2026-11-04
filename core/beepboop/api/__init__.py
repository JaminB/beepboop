"""``/api``: FastAPI routes, session auth, OpenAPI (docs/WEB_UI.md §6, §8)."""

from beepboop.api.app import create_app
from beepboop.api.deps import (
    SESSION_COOKIE,
    Admin,
    ApiDeps,
    Deps,
    Session,
    WebSession,
    current_session,
    require_admin,
    web_scope,
)
from beepboop.api.openapi import openapi_document, write_openapi
from beepboop.api.problem import Problem, problem

__all__ = [
    "SESSION_COOKIE",
    "Admin",
    "ApiDeps",
    "Deps",
    "Problem",
    "Session",
    "WebSession",
    "create_app",
    "current_session",
    "openapi_document",
    "problem",
    "require_admin",
    "web_scope",
    "write_openapi",
]
