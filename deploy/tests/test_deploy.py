"""Static checks for deploy/ (no Docker needed).

These keep deploy/compose.yaml, ports.yaml, the nftables rule, the Caddyfile and
the HA configuration template in step with the canonical shapes in
docs/OPERATIONS.md §3-§4, §7 and docs/HOME_ASSISTANT.md §2.1. They do not prove
the stack runs: `docker compose config` and the V1 probe on the Pi do that.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1]
DEFAULT_SERVICES = {"caddy", "core", "audio", "homeassistant"}

# OPERATIONS §7, 4 GB column (MiB). The sum is the "2.6 GB caps" / "3.1 GB caps" rows.
BUDGET_4G_MIB = {"caddy": 64, "core": 768, "audio": 768, "homeassistant": 1024, "music": 512}
G4_PEAK_MIB = 3.2 * 1024  # ROADMAP G4: peak used RAM < 3.2 GB on a 4 GB Pi

_VAR = re.compile(r"\$\{(?P<name>[A-Z0-9_]+)(?:(?P<op>:-|:\?)(?P<arg>[^}]*))?\}")
_DIGEST_IMAGE = re.compile(r"^[a-z0-9./-]+@\$\{BB_[A-Z]+_DIGEST:\?[^}]+\}$")
_SECRETISH = re.compile(r"secret|token|password|passwd|pin\b|_pin|api_?key|credential", re.I)


def _interpolate(text: str, env: dict[str, str]) -> str:
    """The subset of compose interpolation this file uses: ${VAR}, ${VAR:-d}, ${VAR:?msg}, $$."""

    def sub(m: re.Match[str]) -> str:
        name, op, arg = m["name"], m["op"], m["arg"]
        value = env.get(name, "")
        if value:
            return value
        if op == ":-":
            return arg
        if op == ":?":
            raise KeyError(f"{name}: {arg}")
        return ""

    return _VAR.sub(sub, text).replace("$$", "$")


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return cast(dict[str, Any], data)


def _env_example() -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def _fake_digest_env() -> dict[str, str]:
    env = _env_example()
    for key in env:
        if key.endswith("_DIGEST"):
            env[key] = "sha256:" + "0" * 64
    return env


@pytest.fixture(scope="module")
def raw_compose() -> dict[str, Any]:
    return _load_yaml(DEPLOY / "compose.yaml")


def _interpolate_tree(node: object, env: dict[str, str]) -> Any:
    """Compose interpolates string values after YAML parsing (comments are never touched)."""
    if isinstance(node, str):
        return _interpolate(node, env)
    if isinstance(node, dict):
        return {k: _interpolate_tree(v, env) for k, v in cast(dict[str, object], node).items()}
    if isinstance(node, list):
        return [_interpolate_tree(v, env) for v in cast(list[object], node)]
    return node


@pytest.fixture(scope="module")
def compose(raw_compose: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _interpolate_tree(raw_compose, _fake_digest_env()))


def _services(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return cast(dict[str, dict[str, Any]], doc["services"])


def _mib(size: str) -> float:
    m = re.fullmatch(r"(\d+)([kmg])", size.lower())
    assert m, size
    return int(m[1]) * {"k": 1 / 1024, "m": 1, "g": 1024}[m[2]]


# ------------------------------------------------------------------ compose ---


def test_four_default_services_and_music_profile(compose: dict[str, Any]) -> None:
    services = _services(compose)
    default = {name for name, svc in services.items() if "profiles" not in svc}
    assert default == DEFAULT_SERVICES
    assert services["music"]["profiles"] == ["music"]
    assert compose["name"] == "beepboop"


def test_every_image_is_pinned_by_a_required_digest_variable(raw_compose: dict[str, Any]) -> None:
    for name, svc in _services(raw_compose).items():
        assert _DIGEST_IMAGE.match(svc["image"]), f"{name}: {svc['image']}"


def test_missing_digest_is_a_hard_error(raw_compose: dict[str, Any]) -> None:
    with pytest.raises(KeyError, match="DIGEST"):
        _interpolate_tree(raw_compose, _env_example())  # the example ships empty digests


def test_networking(compose: dict[str, Any]) -> None:
    services = _services(compose)
    assert services["audio"]["network_mode"] == "none"
    for name in ("caddy", "core", "homeassistant", "music"):
        assert services[name]["network_mode"] == "host", name
    assert not any("ports" in svc for svc in services.values()), "host/none networking: no ports:"


def test_audio_device_and_realtime(compose: dict[str, Any]) -> None:
    audio = _services(compose)["audio"]
    assert audio["devices"] == ["/dev/snd:/dev/snd"]
    assert audio["cap_add"] == ["SYS_NICE"]
    assert audio["memswap_limit"] == audio["mem_limit"], "audio never swaps"
    assert audio["oom_score_adj"] == -500
    assert any(t.startswith("/tmp:") for t in audio["tmpfs"])  # noqa: S108 - a tmpfs spec


def test_cpusets(compose: dict[str, Any]) -> None:
    services = _services(compose)
    assert services["audio"]["cpuset"] == "2-3"
    for name in ("caddy", "core", "homeassistant", "music"):
        assert services[name]["cpuset"] == "0-1", name
    assert services["homeassistant"]["cpu_shares"] == 512


def test_memory_limits_match_operations_budget(compose: dict[str, Any]) -> None:
    services = _services(compose)
    limits = {name: _mib(svc["mem_limit"]) for name, svc in services.items()}
    assert limits == BUDGET_4G_MIB
    default_total = sum(limits[s] for s in DEFAULT_SERVICES)
    assert default_total == 2624  # OPERATIONS §3/§7: "2.6 GB caps"
    assert default_total + limits["music"] == 3136  # "3.1 GB caps"
    assert default_total + limits["music"] < G4_PEAK_MIB


def test_env_example_mem_values_equal_compose_defaults(raw_compose: dict[str, Any]) -> None:
    env = _env_example()
    for name, svc in _services(raw_compose).items():
        m = _VAR.fullmatch(svc["mem_limit"])
        assert m, f"{name}: mem_limit must be ${{BB_MEM_*:-default}}"
        assert m["op"] == ":-", name
        assert env[m["name"]] == m["arg"], name


def test_restart_and_logging(compose: dict[str, Any]) -> None:
    for name, svc in _services(compose).items():
        assert svc["restart"] == "unless-stopped", name
        assert svc["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "20m", "max-file": "5"},
        }, name


def test_volumes_live_under_srv_beepboop(compose: dict[str, Any]) -> None:
    allowed_outside = {"/etc/localtime", "/run/dbus"}
    for name, svc in _services(compose).items():
        for vol in svc.get("volumes", []):
            source = str(vol).split(":", 1)[0]
            assert "docker.sock" not in str(vol), f"{name}: no container mounts docker.sock"
            if source == "bb-run":
                continue
            assert source.startswith("/srv/beepboop/") or source in allowed_outside, (name, vol)
    assert set(compose["volumes"]) == {"bb-run"}


def test_healthchecks(compose: dict[str, Any]) -> None:
    services = _services(compose)
    for name in ("core", "audio", "homeassistant"):
        assert "test" in services[name]["healthcheck"], name
    # $$ in the file must reach the container's shell as a single $.
    assert "$(date +%s)" in services["audio"]["healthcheck"]["test"][3]


def test_core_waits_only_for_ha_to_start(compose: dict[str, Any]) -> None:
    dep = _services(compose)["core"]["depends_on"]
    assert dep == {"homeassistant": {"condition": "service_started"}}


# ------------------------------------------------------------------ secrets ---


def test_env_example_holds_no_secrets() -> None:
    env = _env_example()
    for key, value in env.items():
        assert not _SECRETISH.search(key), key
        assert key.startswith("BB_"), key
        if key.endswith("_DIGEST"):
            assert value == "" or re.fullmatch(r"sha256:[0-9a-f]{64}", value), key


def test_compose_environment_holds_no_secrets(raw_compose: dict[str, Any]) -> None:
    for name, svc in _services(raw_compose).items():
        for key in cast(dict[str, str], svc.get("environment", {})):
            assert not _SECRETISH.search(key), (name, key)


def test_dotenv_is_gitignored() -> None:
    gitignore = (DEPLOY.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in gitignore


# ---------------------------------------------------------- ports / nftables ---


def _ports() -> dict[str, Any]:
    return _load_yaml(DEPLOY / "ports.yaml")


def _nft() -> str:
    return (DEPLOY / "nftables" / "beepboop.nft").read_text(encoding="utf-8")


def _nft_literal_ports(proto: str) -> set[int]:
    ports: set[int] = set()
    for m in re.finditer(rf"^\s*{proto} dport (\{{[^}}]*\}}|\d+) accept", _nft(), re.M):
        ports |= {int(p) for p in re.findall(r"\d+", m[1])}
    return ports


def test_ports_entries_are_well_formed() -> None:
    doc = _ports()
    assert doc["schema"] == 1
    entries: list[dict[str, Any]] = [*doc["base"], *doc["loopback_only"], *doc["user"]]
    for profile in cast(dict[str, list[dict[str, Any]]], doc["profiles"]).values():
        entries += profile
    for e in entries:
        assert set(e) == {"port", "proto", "service", "reason"}, e
        assert e["proto"] in {"tcp", "udp"}, e
        assert e["reason"], e
    assert set(doc["profiles"]) == {"music"}  # the one optional profile in v0.1


def test_nftables_base_allowlist_matches_ports_yaml() -> None:
    base = cast(list[dict[str, Any]], _ports()["base"])
    for proto in ("tcp", "udp"):
        declared = {e["port"] for e in base if e["proto"] == proto}
        assert _nft_literal_ports(proto) == declared, proto


def test_loopback_only_ports_are_never_allowed_from_lan() -> None:
    doc = _ports()
    lan = {e["port"] for e in doc["base"]}
    for profile in doc["profiles"].values():
        lan |= {e["port"] for e in profile}
    loopback = {e["port"] for e in doc["loopback_only"]}
    assert not lan & loopback
    assert 8123 in loopback


def test_nftables_is_default_deny_and_drops_8123_first() -> None:
    nft = _nft()
    assert "type filter hook input priority 0; policy drop;" in nft
    assert "flush ruleset" not in re.sub(r"#.*", "", nft), "must not wipe Docker's tables"
    rules = [ln.strip() for ln in nft.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    drop = rules.index(
        'tcp dport 8123 drop comment "Home Assistant is loopback-only; use beepboop"'
    )
    accepts = [i for i, r in enumerate(rules) if r.endswith("accept") or " accept " in r]
    assert accepts[0] < drop < accepts[1], "only `iifname lo accept` may precede the :8123 drop"
    assert rules[accepts[0]] == 'iifname "lo" accept'


# -------------------------------------------------------------------- caddy ---


def _caddy() -> str:
    """The Caddyfile with comment lines and trailing ` # ...` comments removed."""
    text = (DEPLOY / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    lines = [
        "" if ln.lstrip().startswith("#") else ln.split(" #", 1)[0] for ln in text.splitlines()
    ]
    return "\n".join(lines) + "\n"


def test_caddyfile_braces_balance() -> None:
    # Placeholders like {$BB_HOST} and {uri} open and close on one line, so a plain count works.
    depth = 0
    for line in _caddy().splitlines():
        depth += line.count("{") - line.count("}")
        assert depth >= 0
    assert depth == 0


def test_caddyfile_sites_and_gates() -> None:
    caddy = _caddy()
    for site in ("{$BB_HOST} {", "http://{$BB_HOST} {", "{$BB_HOST}:8443 {", "{$BB_HOST}:8445 {"):
        assert f"\n{site}\n" in caddy, site
    assert caddy.count("tls internal") == 3
    assert "uri /api/auth/check?require=admin" in caddy
    assert caddy.count("import admin_gate") == 2
    assert caddy.count("header_up -Cookie") == 2
    assert "@ha_callback path /auth/external/callback*" in caddy
    # HA (:8123) is reached only by the callback matcher and the admin-gated :8443 site.
    assert caddy.count("reverse_proxy 127.0.0.1:8123") == 2
    assert "cert /data/caddy/pki/authorities/local/root.crt" in caddy


# ----------------------------------------------------------- homeassistant ---


def test_ha_configuration_binds_loopback_and_tunes_recorder() -> None:
    cfg = _load_yaml(DEPLOY / "homeassistant" / "configuration.yaml")
    assert cfg["http"]["server_host"] == "127.0.0.1"
    assert cfg["http"]["trusted_proxies"] == ["127.0.0.1"]
    assert cfg["recorder"]["purge_keep_days"] == 3
    assert cfg["recorder"]["commit_interval"] == 30
    urls = cfg["homeassistant"]
    assert urls["internal_url"] == urls["external_url"] == "https://beepboop.local"
