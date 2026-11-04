"""Router registration convention (docs/WEB_UI.md §8).

Each ``beepboop/api/routers/<name>.py`` exposes ``router = APIRouter(prefix="/api/<plural>",
tags=["<name>"])``. Add it to :data:`ROUTERS` here (alphabetical) and ``create_app``
mounts every entry; nothing else registers routes. Operation ids are ``<tag>_<function>``
so the generated TypeScript client has stable names.
"""

from __future__ import annotations

from fastapi import APIRouter

from beepboop.api.routers import (
    approvals,
    auth,
    chat,
    enrollments,
    events,
    grants,
    health,
    home,
    integrations,
    invites,
    me,
    onboarding,
    privacy,
    profiles,
    system,
    turns,
)

ROUTERS: tuple[APIRouter, ...] = (
    approvals.router,
    auth.router,
    home.ca_router,
    chat.router,
    enrollments.router,
    events.router,
    grants.router,
    grants.guest_router,
    health.router,
    home.home_router,
    home.household_router,
    integrations.router,
    invites.router,
    me.router,
    onboarding.router,
    privacy.router,
    profiles.router,
    system.router,
    turns.router,
)

__all__ = ["ROUTERS"]
