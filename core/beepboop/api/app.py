"""FastAPI app factory (docs/WEB_UI.md §8, ARCHITECTURE §2).

``create_app(deps)`` mounts every router in ``beepboop.api.routers.ROUTERS``, installs the
problem+json handlers and the CSRF check, serves ``/api/openapi.json``, and mounts the
static SPA (``ui/build``) with ``index.html`` fallback when the directory exists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from beepboop.api.deps import ApiDeps, csrf_violation
from beepboop.api.problem import install_error_handlers, problem
from beepboop.api.routers import ROUTERS

API_TITLE = "beepboop"

#: WEB_UI §2. The script/style CSP is the hash-mode ``<meta>`` SvelteKit writes into the
#: static HTML; the header adds what a meta tag cannot carry.
SECURITY_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "microphone=()",
}
#: Paths that never fall back to the SPA shell.
NO_FALLBACK = ("/api", "/mcp", "/auth")


class SpaFiles(StaticFiles):
    """``StaticFiles(html=True)`` plus the ``index.html`` fallback for client-side routes,
    immutable caching for hashed assets and ``no-cache`` for the shell (WEB_UI §2)."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        request_path = str(scope.get("path", ""))
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or request_path.startswith(NO_FALLBACK):
                raise
            response = await super().get_response("index.html", scope)
        if request_path.startswith("/_app/immutable/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif isinstance(response, FileResponse) and str(response.path).endswith(".html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(deps: ApiDeps, *, spa_dir: Path | None = None, docs: bool = False) -> FastAPI:
    """``docs=True`` serves Swagger UI at ``/api/docs`` (dev server only: it loads scripts
    from a CDN on the origin that holds the session cookie, so production never does)."""
    app = FastAPI(
        title=API_TITLE,
        version=deps.version,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs" if docs else None,
        redoc_url=None,
    )
    app.state.deps = deps
    install_error_handlers(app)

    @app.middleware("http")
    async def _csrf(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith("/api"):
            code = csrf_violation(request, deps.public_url)
            if code is not None:
                return problem(403, "forbidden", code=code)
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    for router in ROUTERS:
        app.include_router(router)

    if spa_dir is not None and spa_dir.is_dir():
        app.mount("/", SpaFiles(directory=str(spa_dir), html=True), name="spa")
    return app


__all__ = ["API_TITLE", "SECURITY_HEADERS", "SpaFiles", "create_app"]
