"""RFC 9457 ``application/problem+json`` errors (docs/WEB_UI.md §8)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from beepboop.policy.errors import NeedsConfirmation, PolicyDenied

PROBLEM_TYPE = "application/problem+json"


class Problem(BaseModel):
    """The documented error body; ``code`` is stable and translated by the UI."""

    model_config = ConfigDict(extra="allow")

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    code: str | None = None
    fields: dict[str, Any] | None = None


TITLES = {
    400: "bad request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not found",
    409: "conflict",
    410: "gone",
    422: "validation failed",
    429: "too many requests",
    503: "unavailable",
}


def problem(
    status: int,
    title: str,
    *,
    code: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body = Problem(title=title, status=status, code=code, **extra).model_dump(exclude_none=True)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_TYPE, headers=headers)


class ApiError(Exception):
    """Raise from a handler for a problem+json response with a stable ``code`` (WEB_UI §8)."""

    def __init__(
        self,
        status: int,
        code: str,
        *,
        detail: str | None = None,
        fields: dict[str, Any] | None = None,
        retry_after: int | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail
        self.fields = fields
        self.retry_after = retry_after
        self.extra = extra


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_request: Request, exc: ApiError) -> JSONResponse:
        headers = None if exc.retry_after is None else {"Retry-After": str(exc.retry_after)}
        extra = dict(exc.extra)
        if exc.retry_after is not None:
            extra["retry_after"] = exc.retry_after
        return problem(
            exc.status,
            TITLES.get(exc.status, "error"),
            code=exc.code,
            detail=exc.detail,
            fields=exc.fields,
            headers=headers,
            **extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields: dict[str, Any] = {}
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
            fields[".".join(loc) or "_"] = err.get("msg", "invalid")
        return problem(422, "validation failed", code="invalid", fields=fields)

    @app.exception_handler(HTTPException)
    async def _http(_request: Request, exc: HTTPException) -> JSONResponse:
        return problem(
            exc.status_code,
            TITLES.get(exc.status_code, "error"),
            code=str(exc.detail).replace(" ", "_") if exc.detail else None,
            detail=str(exc.detail) if exc.detail else None,
        )

    @app.exception_handler(PolicyDenied)
    async def _denied(_request: Request, exc: PolicyDenied) -> JSONResponse:
        return problem(
            403, "policy denied", code=exc.reason, reason=exc.reason, risk=exc.risk, tool=exc.tool
        )

    @app.exception_handler(NeedsConfirmation)
    async def _confirm(_request: Request, exc: NeedsConfirmation) -> JSONResponse:
        # The nonce never travels in a response body (IDENTITY_AND_TRUST §8.2): SSE only.
        return problem(
            409,
            "needs confirmation",
            code="pending_approval",
            approval_id=exc.approval.id,
            factors=list(exc.factors),
            summary=exc.approval.summary,
        )


__all__ = ["PROBLEM_TYPE", "ApiError", "Problem", "install_error_handlers", "problem"]
