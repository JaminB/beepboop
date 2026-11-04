"""Headless Home Assistant onboarding (HOME_ASSISTANT §3, M0 verification item V2).

On first boot core onboards HA itself so nobody ever sees HA's wizard:

==  ======================================================  ==========================
 #  Request                                                 Auth
==  ======================================================  ==========================
 1  ``GET  /api/onboarding`` (poll until HA answers)        none
 2  ``POST /api/onboarding/users``  → ``auth_code``         none
 3  ``POST /auth/token`` (authorization_code, form body)    none
 4  ``POST /api/onboarding/core_config``                    Bearer (onboarding session)
 5  ``POST /api/onboarding/analytics`` (nothing enabled)    Bearer
 6  ``POST /api/onboarding/integration`` (result unused)    Bearer, must be a refresh-token session
 7  WS ``auth`` then ``auth/long_lived_access_token``       onboarding access token
 8  ``POST /auth/token`` (``action=revoke``, refresh token)  none
 9  ``GET  /api/onboarding``: every step must be done       none
==  ======================================================  ==========================

The step names, payloads and checks were read from HA's source at the pinned
tag 2026.10.3 (``components/onboarding/views.py``, ``components/auth/__init__.py``,
``components/websocket_api/auth.py``); V2 confirms them against the real
container on the Pi (``tools/probes/ha_onboarding/verify_v2.md``).

Secrets: the ``beepboop-system`` password is generated here and stored
*before* the user is created (so a crash can never leave an HA admin whose
password nobody has); the long-lived token is stored at the end. Both go only
into the :class:`~beepboop.secrets.SecretStore` (``owner_module="core"``). The
onboarding refresh token is then revoked server-side (step 8), which also kills
its access token; neither is kept. HA's canonical URLs are not set here: they
live in the managed ``configuration.yaml`` block (HOME_ASSISTANT §2.1).
No token, code or password is ever logged or written to the :class:`OnboardingTrace`.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Literal, cast
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, Field, SecretStr
from structlog.typing import FilteringBoundLogger

from beepboop.ha.ws import HAWebSocket
from beepboop.log import get_logger
from beepboop.secrets import SecretRef, SecretStore


def _log() -> FilteringBoundLogger:
    """The module logger, looked up at call time (``beepboop.log.get_logger`` is lazy too)."""
    return get_logger(__name__)


#: Where onboarding stores what it creates (DATA_AND_PRIVACY §6.1 ``secrets``).
HA_TOKEN_SECRET = SecretRef(name="ha_long_lived_token", owner_module="core")
HA_OWNER_PASSWORD_SECRET = SecretRef(name="ha_owner_password", owner_module="core")

#: The four steps HA reports, in HA's own order (``onboarding/const.py``).
HA_STEPS = ("user", "core_config", "analytics", "integration")

type JsonObject = dict[str, object]
OnboardingStatus = Literal["onboarded", "already_onboarded", "needs_login"]


class OnboardingError(RuntimeError):
    """A request failed. ``step`` names it; HA's error message is included (never a secret)."""

    def __init__(self, step: str, message: str, status: int | None = None) -> None:
        super().__init__(f"HA onboarding step {step!r} failed ({status}): {message}")
        self.step = step
        self.status = status


class OnboardingConfig(BaseModel):
    """Inputs. Defaults are the HOME_ASSISTANT §3 values for a Tier-0 install."""

    ha_url: str = "http://127.0.0.1:8123"
    #: beepboop's canonical URL; HA requires a URL and canonicalises it with a trailing ``/``.
    client_id: str = "https://beepboop.local/"
    name: str = "beepboop"
    username: str = "beepboop-system"
    language: str = "en"
    llat_client_name: str = "beepboop-core"
    llat_lifespan_days: int = Field(default=3650, ge=1)
    ready_timeout_s: float = Field(default=300.0, gt=0)
    request_timeout_s: float = Field(default=10.0, gt=0)

    @property
    def redirect_uri(self) -> str:
        """Same scheme and host as ``client_id``, which is what HA's indieauth check needs."""
        return self.client_id.rstrip("/") + "/?auth_callback=1"

    @property
    def websocket_url(self) -> str:
        """``ws(s)://<host>/api/websocket`` derived from ``ha_url``."""
        parts = urlsplit(self.ha_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}/api/websocket"


class TraceStep(BaseModel):
    """One observed request. Holds *shapes* only: status, timing and key names, never values."""

    step: str
    method: str
    path: str
    status: int | None
    elapsed_ms: float
    response_keys: list[str] = Field(default_factory=list[str])
    note: str | None = None


class OnboardingTrace(BaseModel):
    """The exact sequence as observed; V2 writes this to ``tools/bench/results/``."""

    steps: list[TraceStep] = Field(default_factory=list[TraceStep])

    def add(self, step: TraceStep) -> None:
        """Append and log one step (shape only)."""
        self.steps.append(step)
        _log().info(
            "ha.onboarding.step",
            step=step.step,
            method=step.method,
            path=step.path,
            status=step.status,
            elapsed_ms=round(step.elapsed_ms, 1),
        )


class OnboardingResult(BaseModel):
    """What happened. ``needs_login``: HA was onboarded by someone else (HOME_ASSISTANT §3)."""

    status: OnboardingStatus
    ha_version: str | None = None
    steps_before: dict[str, bool] = Field(default_factory=dict[str, bool])
    steps_after: dict[str, bool] = Field(default_factory=dict[str, bool])
    trace: OnboardingTrace = Field(default_factory=OnboardingTrace)


def generate_owner_password() -> SecretStr:
    """32 random bytes, URL-safe base64 (43 characters)."""
    return SecretStr(secrets.token_urlsafe(32))


class HAOnboarding:
    """Runs the §3 sequence once. Construct with an ``aiohttp`` session and the secret store."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        store: SecretStore,
        config: OnboardingConfig | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self.config = config or OnboardingConfig()
        self.trace = OnboardingTrace()

    # ------------------------------------------------------------- driver --

    async def run(self) -> OnboardingResult:
        """Onboard HA if needed. Idempotent: a stored token short-circuits everything."""
        if await self._store.exists(HA_TOKEN_SECRET):
            _log().info("ha.onboarding.skip", reason="token already stored")
            return OnboardingResult(status="already_onboarded", trace=self.trace)

        before = await self.wait_until_ready()
        if before.get("user", False):
            _log().warning("ha.onboarding.needs_login", reason="HA already has an owner")
            return OnboardingResult(status="needs_login", steps_before=before, trace=self.trace)

        password = generate_owner_password()
        await self._store.put(HA_OWNER_PASSWORD_SECRET, password)
        try:
            auth_code = await self.create_owner(password)
        except OnboardingError as exc:
            await self._store.delete(HA_OWNER_PASSWORD_SECRET)  # no HA user has this password
            if exc.status == 403:  # someone finished the user step between our two requests
                return OnboardingResult(status="needs_login", steps_before=before, trace=self.trace)
            raise
        access_token, refresh_token = await self.exchange_code(auth_code)

        if not before.get("core_config", False):
            await self._post_step("core_config", "/api/onboarding/core_config", access_token, {})
        if not before.get("analytics", False):
            # Marking the step done without calling analytics/preferences leaves every
            # analytics option off; beepboop never enables HA analytics.
            await self._post_step("analytics", "/api/onboarding/analytics", access_token, {})
        if not before.get("integration", False):
            body: JsonObject = {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
            }
            await self._post_step("integration", "/api/onboarding/integration", access_token, body)

        token, ha_version = await self.mint_long_lived_token(access_token)
        await self._store.put(HA_TOKEN_SECRET, token)
        await self.revoke_refresh_token(refresh_token)

        after = await self.get_status("verify")
        missing = [step for step in HA_STEPS if not after.get(step, False)]
        if missing:
            raise OnboardingError("verify", f"steps still pending: {', '.join(missing)}")
        _log().info("ha.onboarding.done", ha_version=ha_version)
        return OnboardingResult(
            status="onboarded",
            ha_version=ha_version,
            steps_before=before,
            steps_after=after,
            trace=self.trace,
        )

    # -------------------------------------------------------------- steps --

    async def get_status(self, step: str = "status") -> dict[str, bool]:
        """``GET /api/onboarding`` → ``{"user": True, "core_config": False, …}``."""
        status, body = await self._request(step, "GET", "/api/onboarding")
        if status != 200 or not isinstance(body, list):
            raise OnboardingError(step, _message(body), status)
        result: dict[str, bool] = {}
        for item in cast("list[object]", body):
            if isinstance(item, dict):
                entry = cast("JsonObject", item)
                result[str(entry.get("step"))] = entry.get("done") is True
        return result

    async def wait_until_ready(self) -> dict[str, bool]:
        """Poll ``GET /api/onboarding`` until HA answers 200 or ``ready_timeout_s`` passes."""
        deadline = time.monotonic() + self.config.ready_timeout_s
        delay = 0.5
        while True:
            try:
                return await self.get_status("wait_ready")
            except (OnboardingError, aiohttp.ClientError, TimeoutError) as exc:
                if time.monotonic() + delay > deadline:
                    msg = f"HA did not become ready within {self.config.ready_timeout_s:.0f} s"
                    raise OnboardingError("wait_ready", msg) from exc
                _log().info("ha.onboarding.waiting", retry_in_s=delay, error=type(exc).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

    async def create_owner(self, password: SecretStr) -> SecretStr:
        """``POST /api/onboarding/users`` → the one-time ``auth_code``."""
        body: JsonObject = {
            "client_id": self.config.client_id,
            "name": self.config.name,
            "username": self.config.username,
            "password": password.get_secret_value(),
            "language": self.config.language,
        }
        status, reply = await self._request("user", "POST", "/api/onboarding/users", json=body)
        code = _field(reply, "auth_code")
        if status != 200 or code is None:
            raise OnboardingError("user", _message(reply), status)
        return SecretStr(code)

    async def exchange_code(self, auth_code: SecretStr) -> tuple[SecretStr, SecretStr | None]:
        """``POST /auth/token`` (``grant_type=authorization_code``) → access and refresh token.

        The refresh token is only kept long enough to revoke it once the
        long-lived token is stored (:meth:`revoke_refresh_token`).
        """
        form = {
            "grant_type": "authorization_code",
            "code": auth_code.get_secret_value(),
            "client_id": self.config.client_id,
        }
        status, reply = await self._request("token", "POST", "/auth/token", data=form)
        access = _field(reply, "access_token")
        if status != 200 or access is None:
            raise OnboardingError("token", _message(reply), status)
        refresh = _field(reply, "refresh_token")
        return SecretStr(access), SecretStr(refresh) if refresh is not None else None

    async def revoke_refresh_token(self, refresh_token: SecretStr | None) -> None:
        """``POST /auth/token`` (``action=revoke``): end the onboarding session in HA.

        HA keeps refresh tokens server-side until revoked; the long-lived token
        replaces this one. Best-effort: HA answers 200 whether or not the token
        existed, and a failure here must not undo a finished onboarding.
        """
        if refresh_token is None:
            return
        form = {"action": "revoke", "token": refresh_token.get_secret_value()}
        try:
            status, _reply = await self._request("revoke", "POST", "/auth/token", data=form)
        except (aiohttp.ClientError, TimeoutError) as exc:
            _log().warning("ha.onboarding.revoke_failed", error=type(exc).__name__)
            return
        if status != 200:
            _log().warning("ha.onboarding.revoke_failed", status=status)

    async def mint_long_lived_token(self, access_token: SecretStr) -> tuple[SecretStr, str | None]:
        """WS ``auth/long_lived_access_token`` on the onboarding session."""
        started = time.perf_counter()
        async with HAWebSocket(
            self._session,
            self.config.websocket_url,
            access_token,
            timeout=self.config.request_timeout_s,
        ) as ws:
            self.trace.add(
                TraceStep(
                    step="ws_auth",
                    method="WS",
                    path="auth",
                    status=None,
                    elapsed_ms=_ms_since(started),
                    response_keys=["type", "ha_version"],
                    note="auth_ok",
                )
            )
            started = time.perf_counter()
            result = await ws.call(
                "auth/long_lived_access_token",
                client_name=self.config.llat_client_name,
                lifespan=self.config.llat_lifespan_days,
            )
            if not isinstance(result, str) or not result:
                raise OnboardingError("long_lived_token", "result was not a token string")
            self.trace.add(
                TraceStep(
                    step="long_lived_token",
                    method="WS",
                    path="auth/long_lived_access_token",
                    status=None,
                    elapsed_ms=_ms_since(started),
                    note=f"lifespan_days={self.config.llat_lifespan_days}",
                )
            )
            return SecretStr(result), ws.ha_version

    # ------------------------------------------------------------ helpers --

    async def _post_step(
        self, step: str, path: str, access_token: SecretStr, body: JsonObject
    ) -> None:
        status, reply = await self._request(step, "POST", path, json=body, token=access_token)
        if status != 200:
            raise OnboardingError(step, _message(reply), status)

    async def _request(
        self,
        step: str,
        method: str,
        path: str,
        *,
        json: JsonObject | None = None,
        data: dict[str, str] | None = None,
        token: SecretStr | None = None,
    ) -> tuple[int, object]:
        headers = {"Authorization": f"Bearer {token.get_secret_value()}"} if token else {}
        started = time.perf_counter()
        async with self._session.request(
            method,
            self.config.ha_url.rstrip("/") + path,
            json=json,
            data=data,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self.config.request_timeout_s),
        ) as response:
            body = await _json_or_none(response)
            self.trace.add(
                TraceStep(
                    step=step,
                    method=method,
                    path=path,
                    status=response.status,
                    elapsed_ms=_ms_since(started),
                    response_keys=_keys(body),
                )
            )
            return response.status, body


async def _json_or_none(response: aiohttp.ClientResponse) -> object:
    try:
        body: object = await response.json(content_type=None)
    except ValueError:
        return None
    return body


def _keys(body: object) -> list[str]:
    """Top-level key names of a JSON object (the trace records shapes, not values)."""
    return sorted(str(key) for key in cast("JsonObject", body)) if isinstance(body, dict) else []


def _field(body: object, key: str) -> str | None:
    if isinstance(body, dict):
        value = cast("JsonObject", body).get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _message(body: object) -> str:
    """HA error text: ``message`` (views) or ``error``/``error_description`` (/auth/token)."""
    if isinstance(body, dict):
        data = cast("JsonObject", body)
        for key in ("message", "error_description", "error"):
            if isinstance(data.get(key), str):
                return str(data[key])
    return "unexpected response"


def _ms_since(started: float) -> float:
    return (time.perf_counter() - started) * 1000
