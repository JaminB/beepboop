"""``ctx.http``: an ``HttpClient`` over httpx limited to the manifest's egress hosts
(docs/MODULES.md §2, §5.3; DATA_AND_PRIVACY §10). Refuses undeclared hosts before any
socket opens, so the privacy ledger is true by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from beepboop.module.types import HttpMethod, ToolError

CONNECT_TIMEOUT_S = 3.0
TOTAL_TIMEOUT_S = 5.0
USER_AGENT = "beepboop/0.1 (+https://github.com/jaminb/beepboop)"


def host_allowed(url: str, allowed: list[str], settings: Mapping[str, Any] | None = None) -> bool:
    """``exact.host``, ``*.domain`` or ``setting:<name>`` (the host of that setting's value)."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    for pattern in allowed:
        p = pattern.lower()
        if p.startswith("setting:"):
            value = (settings or {}).get(p.split(":", 1)[1])
            if isinstance(value, str) and (urlsplit(value).hostname or "").lower() == host:
                return True
            continue
        if host == p or (p.startswith("*.") and host.endswith(p[1:])):
            return True
    return False


class HttpxClient:
    """One client per context; the registry cancels in-flight calls at the tool deadline."""

    def __init__(
        self, *, allowed_hosts: list[str], settings: Mapping[str, Any] | None = None
    ) -> None:
        self._allowed = allowed_hosts
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(TOTAL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        )

    async def request(
        self,
        method: HttpMethod,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,  # noqa: ASYNC109 - the Protocol's signature
    ) -> httpx.Response:
        if not host_allowed(url, self._allowed, self._settings):
            raise ToolError("That host is not in the module's egress list.", code="other")
        # A str/bytes body (an XML PROPFIND/REPORT, an iCalendar PUT) is raw content;
        # a mapping is a form. httpx deprecates passing raw content as ``data=``.
        content: str | bytes | None = data if isinstance(data, str | bytes) else None
        form: Any = None if content is not None else data
        try:
            return await self._client.request(
                method, url, params=params, headers=headers, json=json, data=form,
                content=content,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )  # fmt: skip
        except httpx.TimeoutException as exc:
            raise ToolError(
                "The service did not answer in time.", code="timeout", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ToolError(
                "The service is unreachable.", code="unavailable", retryable=True
            ) from exc

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpx.Response:
        return await self.request("GET", url, params=params, headers=headers, timeout=timeout)

    async def post(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpx.Response:
        return await self.request(
            "POST", url, params=params, headers=headers, json=json, data=data, timeout=timeout
        )

    async def put(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpx.Response:
        return await self.request(
            "PUT", url, params=params, headers=headers, json=json, data=data, timeout=timeout
        )

    async def patch(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpx.Response:
        return await self.request(
            "PATCH", url, params=params, headers=headers, json=json, data=data, timeout=timeout
        )

    async def delete(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpx.Response:
        return await self.request("DELETE", url, params=params, headers=headers, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["CONNECT_TIMEOUT_S", "TOTAL_TIMEOUT_S", "USER_AGENT", "HttpxClient", "host_allowed"]
