"""Headless onboarding against the fake HA (HOME_ASSISTANT §3)."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import aiohttp
import pytest

from beepboop.ha.onboarding import (
    HA_OWNER_PASSWORD_SECRET,
    HA_TOKEN_SECRET,
    HAOnboarding,
    OnboardingConfig,
    OnboardingError,
)
from beepboop.ha.ws import HAAuthInvalidError
from beepboop.log import configure_logging
from beepboop.secrets import SecretStore

if TYPE_CHECKING:
    from .conftest import FakeHA

EXPECTED_SEQUENCE = [
    "GET /api/onboarding",
    "POST /api/onboarding/users",
    "POST /auth/token",
    "POST /api/onboarding/core_config",
    "POST /api/onboarding/analytics",
    "POST /api/onboarding/integration",
    "WS /api/websocket",
    "WS auth/long_lived_access_token",
    "POST /auth/token",
    "GET /api/onboarding",
]


def _onboarding(
    http: aiohttp.ClientSession, store: SecretStore, ha_url: str, **overrides: object
) -> HAOnboarding:
    config = OnboardingConfig.model_validate({"ha_url": ha_url, "ready_timeout_s": 5, **overrides})
    return HAOnboarding(http, store, config)


async def test_full_sequence(
    fake_ha: FakeHA, ha_url: str, http: aiohttp.ClientSession, store: SecretStore
) -> None:
    result = await _onboarding(http, store, ha_url).run()

    assert result.status == "onboarded"
    assert result.ha_version == "2026.10.3"
    assert result.steps_before == dict.fromkeys(
        ["user", "core_config", "analytics", "integration"], False
    )
    assert all(result.steps_after.values())
    assert fake_ha.requests == EXPECTED_SEQUENCE
    assert [step.step for step in result.trace.steps] == [
        "wait_ready",
        "user",
        "token",
        "core_config",
        "analytics",
        "integration",
        "ws_auth",
        "long_lived_token",
        "revoke",
        "verify",
    ]
    # One LLAT, minted with the documented name and 10-year lifespan, is what we stored.
    assert fake_ha.llats == [
        {"token": fake_ha.llats[0]["token"], "lifespan": 3650, "client_name": "beepboop-core"}
    ]
    assert (await store.get(HA_TOKEN_SECRET)).get_secret_value() == fake_ha.llats[0]["token"]
    password = (await store.get(HA_OWNER_PASSWORD_SECRET)).get_secret_value()
    assert fake_ha.users == {"beepboop-system": password}
    assert len(password) >= 43


async def test_secrets_never_reach_logs_or_trace(
    fake_ha: FakeHA, ha_url: str, http: aiohttp.ClientSession, store: SecretStore
) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)

    result = await _onboarding(http, store, ha_url).run()

    logged = stream.getvalue()
    trace = result.model_dump_json()
    assert "ha.onboarding.step" in logged, "the run must actually have logged"
    issued = fake_ha.issued_secrets()
    assert len(issued) >= 5  # password, access, refresh, auth code, LLAT
    for value in issued:
        assert value not in logged
        assert value not in trace


async def test_second_run_is_a_no_op(
    fake_ha: FakeHA, ha_url: str, http: aiohttp.ClientSession, store: SecretStore
) -> None:
    await _onboarding(http, store, ha_url).run()
    fake_ha.requests.clear()

    result = await _onboarding(http, store, ha_url).run()

    assert result.status == "already_onboarded"
    assert fake_ha.requests == []


async def test_ha_onboarded_by_someone_else_needs_login(
    fake_ha: FakeHA, ha_url: str, http: aiohttp.ClientSession, store: SecretStore
) -> None:
    fake_ha.done.extend(["user", "core_config", "analytics", "integration"])

    result = await _onboarding(http, store, ha_url).run()

    assert result.status == "needs_login"
    assert not await store.exists(HA_OWNER_PASSWORD_SECRET)
    assert fake_ha.requests == ["GET /api/onboarding"]


async def test_racing_user_step_needs_login_and_drops_password(
    fake_ha: FakeHA, ha_url: str, http: aiohttp.ClientSession, store: SecretStore
) -> None:
    fake_ha.race_user_step = True

    result = await _onboarding(http, store, ha_url).run()

    assert result.status == "needs_login"
    assert not await store.exists(HA_OWNER_PASSWORD_SECRET)


async def test_waits_for_ha_to_start(
    fake_ha: FakeHA, ha_url: str, http: aiohttp.ClientSession, store: SecretStore
) -> None:
    fake_ha.not_ready_polls = 2

    result = await _onboarding(http, store, ha_url).run()

    assert result.status == "onboarded"
    assert fake_ha.requests[:3] == ["GET /api/onboarding"] * 3


async def test_gives_up_when_ha_never_answers(
    fake_ha: FakeHA, ha_url: str, http: aiohttp.ClientSession, store: SecretStore
) -> None:
    fake_ha.not_ready_polls = 1000

    with pytest.raises(OnboardingError) as caught:
        await _onboarding(http, store, ha_url, ready_timeout_s=1).run()

    assert caught.value.step == "wait_ready"


async def test_rejected_code_is_a_token_error(
    fake_ha: FakeHA, ha_url: str, http: aiohttp.ClientSession, store: SecretStore
) -> None:
    fake_ha.reject_codes = True

    with pytest.raises(OnboardingError) as caught:
        await _onboarding(http, store, ha_url).run()

    assert caught.value.step == "token"
    assert caught.value.status == 400
    assert "Invalid code" in str(caught.value)
    assert not await store.exists(HA_TOKEN_SECRET)
    # The password stays stored: HA now has a beepboop-system user with it (break-glass).
    assert await store.exists(HA_OWNER_PASSWORD_SECRET)


async def test_ws_auth_invalid_is_not_retried(
    fake_ha: FakeHA, ha_url: str, http: aiohttp.ClientSession, store: SecretStore
) -> None:
    fake_ha.reject_ws_auth = True

    with pytest.raises(HAAuthInvalidError):
        await _onboarding(http, store, ha_url).run()

    assert fake_ha.requests.count("WS /api/websocket") == 1
    assert not await store.exists(HA_TOKEN_SECRET)


async def test_onboarding_refresh_token_is_revoked(
    fake_ha: FakeHA, ha_url: str, http: aiohttp.ClientSession, store: SecretStore
) -> None:
    await _onboarding(http, store, ha_url).run()

    # The only session left in HA is the long-lived token we stored (HOME_ASSISTANT §3).
    assert len(fake_ha.revoked_refresh_tokens) == 1
    assert fake_ha.refresh_tokens == set()
    assert fake_ha.access_tokens == {fake_ha.llats[0]["token"]}
    assert "WS config/core/update" not in fake_ha.requests  # URLs are YAML-owned (§2.1)


async def test_failed_revocation_does_not_undo_onboarding(
    fake_ha: FakeHA, ha_url: str, http: aiohttp.ClientSession, store: SecretStore
) -> None:
    fake_ha.fail_revoke = True

    result = await _onboarding(http, store, ha_url).run()

    assert result.status == "onboarded"
    assert await store.exists(HA_TOKEN_SECRET)
    assert fake_ha.requests.count("POST /auth/token") == 2  # exchange + one revoke, no retry


def test_config_has_no_url_settings() -> None:
    # HA's canonical URLs live only in the managed configuration.yaml (hard rule 10).
    assert "internal_url" not in OnboardingConfig.model_fields


def test_config_derives_redirect_and_websocket_urls() -> None:
    config = OnboardingConfig(ha_url="https://ha.example:8123", client_id="https://beepboop.local/")
    assert config.redirect_uri == "https://beepboop.local/?auth_callback=1"
    assert config.websocket_url == "wss://ha.example:8123/api/websocket"
    assert OnboardingConfig().websocket_url == "ws://127.0.0.1:8123/api/websocket"
