#!/usr/bin/env python3
"""The host side of ``/usr/local/bin/beepboop`` (docs/OPERATIONS.md §5, §9, §10, §12).

Runs on the Pi's own Python (Raspberry Pi OS: 3.11+; stdlib plus ``python3-yaml``, which
``install.sh`` installs), never in a container, because these commands need Docker, the
host filesystem or a stopped core. Core never gains Docker privileges and no container
mounts ``docker.sock``.

Subcommands (CLAUDE.md §9 is the registry):

* ``doctor [--preflight]``: listening sockets vs ``deploy/ports.yaml``, cgroup memory
  enforcement, nftables, HA loopback bind; ``--preflight`` adds install step 1.
* ``update [--to <release>] [--manifest <file|url>]``: snapshot, pull by digest, switch,
  verify, roll back (OPERATIONS §9).
* ``db restore <bundle> [--identity <file>]``: stop core, restore in a one-off core
  container, start, verify (DATA_AND_PRIVACY §11).
* ``logs [--turn <id>]``: tail compose logs; ``--turn`` joins both containers' lines.
* ``models verify | update``: re-hash or re-download the weights from the audio image.
* ``host-json``: what ``beepboop-host.timer`` runs every 10 s (writes ``run/host.json``
  and restarts containers Docker reports unhealthy).

Everything else is forwarded into core with ``docker compose exec core beepboop …``.
Every external effect goes through :class:`Runner`, so the logic is tested on any OS
with a fake (``deploy/tests/test_host.py``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

ROOT = Path(os.environ.get("BB_ROOT", "/srv/beepboop"))
HEALTH_URL = "http://127.0.0.1:8080/api/health"
RELEASES_URL = "https://beepboop.dev/releases/{channel}.json"  # planned domain
DIGEST_KEYS = (
    "BB_CADDY_DIGEST",
    "BB_CORE_DIGEST",
    "BB_AUDIO_DIGEST",
    "BB_HA_DIGEST",
    "BB_MUSIC_DIGEST",
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
MIN_FREE_BYTES = 3 * 1000**3
CORE_VERIFY_S = 180
HA_VERIFY_S = 420
#: DHCP clients hold these sockets; they are not services (OPERATIONS §2 step 8).
CLIENT_SOCKETS = frozenset({("udp", 68), ("udp", 546)})
PRE_UPDATE_KEEP_S = 7 * 86_400


# ------------------------------------------------------------------ plumbing --


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, input_text: str | None = None, timeout: float | None = None
    ) -> Result: ...


def system_runner(
    argv: Sequence[str], *, input_text: str | None = None, timeout: float | None = None
) -> Result:
    try:
        done = subprocess.run(  # noqa: S603 - argv lists built here, never a shell string
            list(argv), input=input_text, capture_output=True, text=True, timeout=timeout,
            check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(127, "", f"{type(exc).__name__}: {exc}")
    return Result(done.returncode, done.stdout, done.stderr)


def http_get_json(url: str, timeout: float = 4.0) -> dict[str, Any] | None:
    if not url.startswith(("http://127.0.0.1", "https://")):
        msg = f"refusing to fetch {url}"
        raise ValueError(msg)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - scheme checked
            body = resp.read()
    except OSError:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


@dataclass
class Host:
    """The device as the host tool sees it. Tests build one over a temp dir and fakes."""

    root: Path = ROOT
    run: Runner = system_runner
    http: Callable[[str], dict[str, Any] | None] = http_get_json
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    proc: Path = Path("/proc")
    reboot_flag: Path = Path("/var/run/reboot-required")
    sys_root: Path = Path("/sys")
    out: Callable[[str], None] = field(default=lambda line: print(line, flush=True))  # noqa: T201

    @property
    def deploy(self) -> Path:
        return self.root / "deploy"

    @property
    def env_file(self) -> Path:
        return self.deploy / ".env"

    @property
    def compose_file(self) -> Path:
        return self.deploy / "compose.yaml"

    def compose(self, *args: str, env_file: Path | None = None) -> list[str]:
        argv = ["docker", "compose", "-f", str(self.compose_file)]
        if env_file is not None:
            argv += ["--env-file", str(env_file)]
        return [*argv, *args]

    def stamp(self) -> str:
        return self.clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------- .env --


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Rewrite ``KEY=value`` lines in place (comments and order kept), atomically."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        key = raw.partition("=")[0].strip()
        if "=" in raw and not raw.lstrip().startswith("#") and key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(raw)
    out += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)


# -------------------------------------------------------------------- doctor --


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str = ""


@dataclass(frozen=True)
class Socket:
    proto: str
    addr: str
    port: int

    @property
    def loopback(self) -> bool:
        return self.addr.startswith("127.") or self.addr in ("::1", "localhost")


def _hex_addr(raw: str) -> str:
    """Decode a /proc/net address (little-endian per 32-bit word) to text."""
    if len(raw) == 8:
        return ".".join(str(b) for b in bytes.fromhex(raw)[::-1])
    words = [bytes.fromhex(raw[i : i + 8])[::-1] for i in range(0, 32, 8)]
    data = b"".join(words)
    if data == bytes(15) + b"\x01":
        return "::1"
    if data[:12] == bytes(10) + b"\xff\xff":  # v4-mapped
        return ".".join(str(b) for b in data[12:])
    if data == bytes(16):
        return "::"
    return ":".join(data[i : i + 2].hex() for i in range(0, 16, 2))


def listening_sockets(proc: Path) -> list[Socket]:
    """TCP sockets in LISTEN and bound UDP sockets, from /proc/net (host namespace)."""
    found: set[Socket] = set()
    for proto, files, state in (
        ("tcp", ("tcp", "tcp6"), "0A"),
        ("udp", ("udp", "udp6"), "07"),
    ):
        for name in files:
            path = proc / "net" / name
            if not path.exists():
                continue
            for line in path.read_text().splitlines()[1:]:
                parts = line.split()
                if len(parts) < 4 or parts[3] != state:
                    continue
                addr, _, port_hex = parts[1].partition(":")
                found.add(Socket(proto, _hex_addr(addr), int(port_hex, 16)))
    return sorted(found, key=lambda s: (s.proto, s.port, s.addr))


def load_ports(path: Path) -> dict[str, Any]:
    import yaml  # python3-yaml on the host (install.sh step 2)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or cast(dict[str, Any], data).get("schema") != 1:
        msg = f"{path}: expected schema: 1"
        raise ValueError(msg)
    return cast(dict[str, Any], data)


def _entries(items: object) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    if isinstance(items, list):
        for e in cast(list[Any], items):
            if isinstance(e, dict):
                d = cast(dict[str, Any], e)
                out.add((str(d["proto"]), int(d["port"])))
    return out


def allowed_ports(ports: dict[str, Any], profiles: Iterable[str]) -> set[tuple[str, int]]:
    allowed = _entries(ports.get("base")) | _entries(ports.get("user"))
    by_profile = cast(dict[str, Any], ports.get("profiles") or {})
    for p in profiles:
        allowed |= _entries(by_profile.get(p))
    return allowed


def enabled_profiles(env: dict[str, str]) -> list[str]:
    raw = env.get("COMPOSE_PROFILES", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def check_ports(sockets: list[Socket], ports: dict[str, Any], profiles: list[str]) -> list[Check]:
    """``doctor`` flags every non-loopback listener not in ports.yaml (ROADMAP v0.1 #9)."""
    allowed = allowed_ports(ports, profiles) | CLIENT_SOCKETS
    loopback_only = _entries(ports.get("loopback_only"))
    checks: list[Check] = []
    for s in sockets:
        key = (s.proto, s.port)
        if s.loopback:
            continue
        if key in loopback_only:
            checks.append(
                Check(
                    f"port {s.port}/{s.proto}",
                    "fail",
                    f"must be loopback-only but listens on {s.addr}",
                )
            )
        elif key not in allowed:
            checks.append(
                Check(
                    f"port {s.port}/{s.proto}",
                    "fail",
                    f"listening on {s.addr} but not declared in deploy/ports.yaml; "
                    "add it with a reason, or stop it",
                )
            )
    if not checks:
        checks.append(Check("listening ports", "ok", "every LAN listener is declared"))
    return checks


def check_cgroup_memory(proc: Path, sys_root: Path) -> Check:
    cmdline = (proc / "cmdline").read_text() if (proc / "cmdline").exists() else ""
    controllers = sys_root / "fs" / "cgroup" / "cgroup.controllers"
    has_ctrl = controllers.exists() and "memory" in controllers.read_text().split()
    flags = "cgroup_enable=memory" in cmdline and "cgroup_memory=1" in cmdline
    if has_ctrl:
        return Check("memory limits", "ok", "cgroup memory controller enabled")
    if flags:
        return Check("memory limits", "warn", "flags set; reboot to apply")
    return Check(
        "memory limits",
        "fail",
        "memory limits not enforced: add cgroup_enable=memory cgroup_memory=1 "
        "swapaccount=1 to /boot/firmware/cmdline.txt and reboot",
    )


def check_nftables(run: Runner) -> Check:
    r = run(["nft", "list", "table", "inet", "beepboop"])
    if r.ok and "tcp dport 8123 drop" in r.stdout:
        return Check("firewall", "ok", "inet beepboop loaded; LAN :8123 dropped")
    return Check("firewall", "fail", "nftables table inet beepboop missing; re-run install.sh")


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def preflight(host: Host, run: Runner) -> list[Check]:
    """Install step 1 (OPERATIONS §5), re-runnable."""
    checks: list[Check] = []
    arch = run(["uname", "-m"]).stdout.strip()
    checks.append(Check("architecture", "ok" if arch == "aarch64" else "fail", arch or "?"))
    model_path = host.proc / "device-tree" / "model"
    model = model_path.read_text(errors="replace").strip("\x00\n") if model_path.exists() else ""
    checks.append(Check("board", "ok" if "Raspberry Pi 4" in model else "fail", model or "?"))
    meminfo = (host.proc / "meminfo").read_text() if (host.proc / "meminfo").exists() else ""
    m = re.search(r"MemTotal:\s+(\d+) kB", meminfo)
    mem_gib = int(m.group(1)) / 1024**2 if m else 0.0
    checks.append(Check("memory", "ok" if mem_gib >= 3.4 else "fail", f"{mem_gib:.1f} GiB"))
    root_src = run(["findmnt", "-n", "-o", "SOURCE", "/"]).stdout.strip()
    on_sd = "mmcblk0" in root_src
    checks.append(Check("root on SSD", "fail" if on_sd else "ok", root_src or "?"))
    free = shutil.disk_usage(host.root if host.root.exists() else "/").free
    checks.append(
        Check("disk free", "ok" if free >= MIN_FREE_BYTES else "fail", f"{free / 1e9:.1f} GB")
    )
    throttled = run(["vcgencmd", "get_throttled"]).stdout.strip().partition("=")[2]
    checks.append(
        Check("throttling", "ok" if throttled == "0x0" else "fail", throttled or "unknown")
    )
    milli = _read_int(host.sys_root / "class" / "thermal" / "thermal_zone0" / "temp")
    if milli is None:
        checks.append(Check("temperature", "warn", "unknown"))
    else:
        checks.append(
            Check("temperature", "ok" if milli < 70_000 else "fail", f"{milli / 1000:.0f} C")
        )
    cards = host.proc / "asound" / "cards"
    has_array = cards.exists() and "XVF3800" in cards.read_text()
    checks.append(Check("microphone array", "ok" if has_array else "fail", "XVF3800"))
    return checks


def doctor(host: Host, *, with_preflight: bool) -> int:
    checks: list[Check] = []
    if with_preflight:
        checks += preflight(host, host.run)
    env = read_env(host.env_file)
    ports = load_ports(host.deploy / "ports.yaml")
    checks += check_ports(listening_sockets(host.proc), ports, enabled_profiles(env))
    checks.append(check_cgroup_memory(host.proc, host.sys_root))
    checks.append(check_nftables(host.run))
    # XVF3800 firmware vs deploy/firmware.lock needs the vendor control tool on the Pi.
    checks.append(Check("array firmware", "skip", "pending on Pi: needs the XVF3800 host tool"))
    for c in checks:
        host.out(f"{c.status:<4} {c.name}: {c.detail}")
    return 1 if any(c.status == "fail" for c in checks) else 0


# -------------------------------------------------------------------- update --


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseManifest:
    release: str
    digests: dict[str, str]
    schema_version: int | None = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> ReleaseManifest:
        digests = cast(dict[str, Any], data.get("digests") or {})
        clean: dict[str, str] = {}
        for key, value in digests.items():
            if key not in DIGEST_KEYS or not isinstance(value, str) or not _DIGEST.match(value):
                msg = f"release manifest has a bad digest entry: {key}"
                raise UpdateError(msg)
            clean[key] = value
        for required in ("BB_CORE_DIGEST", "BB_AUDIO_DIGEST", "BB_HA_DIGEST", "BB_CADDY_DIGEST"):
            if required not in clean:
                msg = f"release manifest is missing {required}"
                raise UpdateError(msg)
        schema = data.get("schema_version")
        return cls(
            release=str(data.get("release", "?")),
            digests=clean,
            schema_version=schema if isinstance(schema, int) else None,
        )


def load_manifest(source: str, fetch: Callable[[str], dict[str, Any] | None]) -> ReleaseManifest:
    if source.startswith("https://"):
        data = fetch(source)
    else:
        try:
            data = cast(dict[str, Any], json.loads(Path(source).read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            msg = f"cannot read release manifest {source}"
            raise UpdateError(msg) from exc
    if not isinstance(data, dict):
        msg = f"cannot fetch release manifest {source}"
        raise UpdateError(msg)
    return ReleaseManifest.parse(data)


def _schema_version(host: Host) -> int | None:
    health = host.http(HEALTH_URL)
    value = health.get("schema_version") if health else None
    return value if isinstance(value, int) else None


def _container_health(host: Host, name: str) -> str:
    r = host.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", name])
    return r.stdout.strip() if r.ok else "missing"


def verify(host: Host, *, check_ha: bool) -> tuple[bool, str]:
    """Core and audio within 180 s; HA within 420 s (OPERATIONS §9)."""
    start = host.monotonic()

    def wait(pred: Callable[[], bool], limit: float) -> bool:
        deadline = host.monotonic() + limit
        while True:
            if pred():
                return True
            if host.monotonic() >= deadline:
                return False
            host.sleep(5)

    core_ok = wait(
        lambda: (h := host.http(HEALTH_URL)) is not None and h.get("status") == "ok",
        CORE_VERIFY_S,
    )
    if not core_ok:
        return False, "core /api/health did not answer ok"
    if not wait(lambda: _container_health(host, "beepboop-audio") == "healthy", CORE_VERIFY_S):
        return False, "audio heartbeat not healthy"
    turn = host.run(
        host.compose("exec", "-T", "core", "beepboop", "chat", "--as", "guest", "--dry-run",
                     "what time is it"),
        timeout=60,
    )  # fmt: skip
    if not turn.ok:
        return False, "synthetic fast-path turn failed"
    if check_ha and not wait(
        lambda: _container_health(host, "homeassistant") == "healthy",
        max(0.0, HA_VERIFY_S - (host.monotonic() - start)),
    ):
        return False, "Home Assistant did not become healthy"
    return True, "ok"


def _copy_ha(ha: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if (ha / ".storage").is_dir():
        shutil.copytree(ha / ".storage", dest / ".storage", dirs_exist_ok=True)
    if (ha / "configuration.yaml").exists():
        shutil.copy2(ha / "configuration.yaml", dest / "configuration.yaml")


def _restore_ha(snapshot: Path, ha: Path) -> None:
    if (snapshot / ".storage").is_dir():
        shutil.rmtree(ha / ".storage", ignore_errors=True)
        shutil.copytree(snapshot / ".storage", ha / ".storage")
    if (snapshot / "configuration.yaml").exists():
        shutil.copy2(snapshot / "configuration.yaml", ha / "configuration.yaml")


def _restore_db(host: Host, snapshot_db: Path) -> None:
    db = host.root / "core" / "beepboop.db"
    host.run(host.compose("stop", "core"))
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    shutil.copy2(snapshot_db, db)


def write_update_status(host: Host, status: dict[str, Any]) -> None:
    """``run/update.json``: what core shows on the Updates page and announces."""
    run_dir = host.root / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp = run_dir / "update.json.tmp"
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(run_dir / "update.json")


def update(host: Host, *, manifest: ReleaseManifest) -> int:
    """OPERATIONS §9 step 3: preflight, snapshot, pull, switch, verify, rollback, cleanup."""
    stamp = host.stamp()
    log: list[str] = []

    def say(line: str) -> None:
        log.append(line)
        host.out(line)

    def finish(ok: bool, message: str) -> int:
        write_update_status(host, {"at": host.clock().isoformat(), "ok": ok,
                                   "release": manifest.release, "message": message,
                                   "log": log[-50:]})  # fmt: skip
        return 0 if ok else 1

    # preflight
    free = shutil.disk_usage(host.root).free
    if free < MIN_FREE_BYTES:
        say(f"preflight: only {free / 1e9:.1f} GB free; need 3 GB")
        return finish(False, "not enough disk")
    if host.reboot_flag.exists():
        say("preflight: a reboot is pending; reboot first")
        return finish(False, "reboot pending")
    if "Leap status     : Normal" not in host.run(["chronyc", "tracking"]).stdout:
        say("preflight: clock not synced (chronyc tracking)")
        return finish(False, "clock not synced")

    current = read_env(host.env_file)
    wanted = {k: v for k, v in manifest.digests.items() if current.get(k) != v}
    if not wanted:
        say(f"already on {manifest.release}")
        return finish(True, "already up to date")
    ha_changes = "BB_HA_DIGEST" in wanted
    schema_before = _schema_version(host)
    downgrade_db: Path | None = None
    if (
        manifest.schema_version is not None
        and schema_before is not None
        and manifest.schema_version < schema_before
    ):
        downgrade_db = _snapshot_at_schema(host, manifest.schema_version)
        if downgrade_db is None:
            say(
                f"downgrade: {manifest.release} needs schema {manifest.schema_version} and no "
                "pre-update snapshot has it; restore a backup instead (beepboop db restore)"
            )
            return finish(False, "no snapshot for the downgrade")

    # snapshot
    snap = host.root / "backups" / f"pre-update-{stamp}"
    snap.mkdir(parents=True, exist_ok=True)
    r = host.run(
        host.compose("exec", "-T", "core", "beepboop", "db", "backup", "--out",
                     f"/backups/pre-update-{stamp}/beepboop.db"),
        timeout=600,
    )  # fmt: skip
    if not r.ok:
        say("snapshot: database snapshot failed; nothing changed")
        return finish(False, "snapshot failed")
    (snap / "schema_version").write_text(str(schema_before or ""), encoding="utf-8")
    shutil.copy2(host.env_file, snap / ".env")
    shutil.copy2(host.compose_file, snap / "compose.yaml")
    ha = host.root / "ha"
    if ha_changes:
        host.run(host.compose("stop", "homeassistant"), timeout=120)
        _copy_ha(ha, snap / "ha")
        recorder = ha / "home-assistant_v2.db"
        if recorder.exists():
            recorder.replace(ha / f"home-assistant_v2.db.pre-update-{stamp}")
    say(f"snapshot: {snap}")

    # pull with the new digests, before touching .env
    next_env = host.deploy / ".env.next"
    shutil.copy2(host.env_file, next_env)
    write_env(next_env, wanted)
    if not host.run(host.compose("pull", env_file=next_env), timeout=1800).ok:
        next_env.unlink(missing_ok=True)
        if ha_changes:
            host.run(host.compose("start", "homeassistant"))
        say("pull: failed; nothing changed")
        return finish(False, "pull failed")

    # switch
    next_env.replace(host.env_file)
    if downgrade_db is not None:
        # Migrations are forward-only: the older release gets the snapshot taken before
        # the update that migrated past it. Changes since then are lost, and we say so.
        _restore_db(host, downgrade_db)
        say(f"downgrade: database restored from {downgrade_db.parent.name}")
    host.run(host.compose("up", "-d", "--remove-orphans"), timeout=600)
    ok, why = verify(host, check_ha=True)
    if ok:
        say(f"verify: ok, now on {manifest.release}")
        _cleanup(host, keep=snap, previous=current)
        return finish(True, f"updated to {manifest.release}")

    # rollback
    say(f"verify: {why}; rolling back")
    migrated = downgrade_db is not None or _migrated(host, schema_before)
    shutil.copy2(snap / ".env", host.env_file)
    if migrated and (snap / "beepboop.db").exists():
        _restore_db(host, snap / "beepboop.db")
        say("rollback: database restored from the pre-update snapshot")
    if ha_changes:
        host.run(host.compose("stop", "homeassistant"))
        _restore_ha(snap / "ha", ha)
        say("rollback: Home Assistant config restored")
    host.run(host.compose("up", "-d", "--remove-orphans"), timeout=600)
    rolled_ok, _ = verify(host, check_ha=False)
    say("rollback: " + ("the update failed and I rolled back" if rolled_ok else "FAILED"))
    return finish(False, "the update failed and I rolled back")


def _migrated(host: Host, schema_before: int | None) -> bool:
    """True if the new core may have migrated: its schema moved, or it never answered."""
    after = _schema_version(host)
    return after is None or schema_before is None or after != schema_before


def _snapshot_at_schema(host: Host, schema: int) -> Path | None:
    """The newest pre-update snapshot whose database is at ``schema``."""
    found: list[Path] = []
    for d in (host.root / "backups").glob("pre-update-*"):
        marker = d / "schema_version"
        at_schema = marker.exists() and marker.read_text(encoding="utf-8").strip() == str(schema)
        if at_schema and (d / "beepboop.db").exists():
            found.append(d / "beepboop.db")
    return max(found, key=lambda p: p.parent.name) if found else None


def _cleanup(host: Host, *, keep: Path, previous: dict[str, str]) -> None:
    """Prune images except the previous release's; drop older pre-update snapshots."""
    keep_digests = {v for k, v in previous.items() if k in DIGEST_KEYS and v}
    keep_digests |= {v for k, v in read_env(host.env_file).items() if k in DIGEST_KEYS and v}
    listed = host.run(["docker", "images", "--digests", "--format", "{{.ID}} {{.Digest}}"])
    for line in listed.stdout.splitlines():
        image_id, _, digest = line.partition(" ")
        if digest.startswith("sha256:") and digest not in keep_digests:
            host.run(["docker", "rmi", image_id])
    # The previous pre-update snapshots go after a successful update (DATA_AND_PRIVACY §11);
    # keep those a downgrade could still need (their schema differs from the current one).
    current_schema = _schema_version(host)
    for old in (host.root / "backups").glob("pre-update-*"):
        if old == keep or not old.is_dir():
            continue
        marker = old / "schema_version"
        schema = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        if schema == str(current_schema):
            shutil.rmtree(old, ignore_errors=True)


def prune_stale_snapshots(host: Host) -> int:
    """Pre-update snapshots older than 7 days go (DATA_AND_PRIVACY §11)."""
    removed = 0
    now = host.clock().timestamp()
    for old in (host.root / "backups").glob("pre-update-*"):
        if old.is_dir() and now - old.stat().st_mtime > PRE_UPDATE_KEEP_S:
            shutil.rmtree(old, ignore_errors=True)
            removed += 1
    return removed


# ---------------------------------------------------------------- db restore --


def db_restore(host: Host, bundle: Path, identity: str) -> int:
    """Stop core, run core's ``db restore`` in a one-off container, start, verify."""
    if not bundle.is_file():
        host.out(f"no such bundle: {bundle}")
        return 2
    inbox = host.root / "backups" / "restore"
    inbox.mkdir(parents=True, exist_ok=True)
    staged = inbox / bundle.name
    if staged.resolve() != bundle.resolve():
        shutil.copy2(bundle, staged)
    host.run(host.compose("stop", "core", "homeassistant"), timeout=180)
    r = host.run(
        host.compose("run", "--rm", "--no-deps", "-T", "core", "beepboop", "db", "restore",
                     f"/backups/restore/{bundle.name}", "--identity", "-"),
        input_text=identity, timeout=900,
    )  # fmt: skip
    host.out(r.stdout.strip() or r.stderr.strip())
    host.run(host.compose("up", "-d"), timeout=600)
    if not r.ok:
        host.out("restore refused; the previous database is unchanged")
        return 1
    ok, why = verify(host, check_ha=True)
    host.out("restore verified" if ok else f"restore applied but verify failed: {why}")
    return 0 if ok else 1


# ------------------------------------------------------------------ host.json --


def _float_or_none(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _mib(text: str) -> float | None:
    m = re.match(r"^\s*([\d.]+)\s*([KMG]i?B)", text)
    if not m:
        return None
    scale = {"KiB": 1 / 1024, "KB": 1 / 1024, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1024}
    return round(float(m.group(1)) * scale[m.group(2)], 1)


def collect_host_json(host: Host) -> dict[str, Any]:
    """The ``run/host.json`` document (fields: OPERATIONS §5)."""
    containers: dict[str, dict[str, Any]] = {}
    ps = host.run(["docker", "ps", "-a", "--format", "{{json .}}"])
    for line in ps.stdout.splitlines():
        try:
            row = cast(dict[str, Any], json.loads(line))
        except ValueError:
            continue
        name = str(row.get("Names", ""))
        status = str(row.get("Status", ""))
        health = "healthy" if "(healthy)" in status else "unhealthy" if "(unhealthy)" in status \
            else "starting" if "(health: starting)" in status else ""  # fmt: skip
        containers[name] = {"state": str(row.get("State", "")), "health": health}
    stats = host.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"])
    for line in stats.stdout.splitlines():
        try:
            row = cast(dict[str, Any], json.loads(line))
        except ValueError:
            continue
        name = str(row.get("Name", ""))
        used, _, limit = str(row.get("MemUsage", "")).partition("/")
        entry = containers.setdefault(name, {"state": "running", "health": ""})
        entry["mem_mb"] = _mib(used)
        entry["mem_limit_mb"] = _mib(limit)
        entry["cpu_pct"] = _float_or_none(str(row.get("CPUPerc", "")).rstrip("%"))
    throttled = host.run(["vcgencmd", "get_throttled"]).stdout.strip().partition("=")[2] or None
    milli = _read_int(host.sys_root / "class" / "thermal" / "thermal_zone0" / "temp")
    chrony = host.run(["chronyc", "-c", "tracking"]).stdout.split(",")
    offset = _float_or_none(chrony[4]) if len(chrony) > 4 else None
    env = read_env(host.env_file)
    return {
        "written_at": host.clock().isoformat(),
        "containers": containers,
        "throttled": throttled,
        "temperature_c": milli / 1000 if milli is not None else None,
        "disk_free_bytes": shutil.disk_usage(host.root).free if host.root.exists() else None,
        "reboot_pending": host.reboot_flag.exists(),
        "chrony_offset_s": offset,
        "release": {k: env.get(k, "") for k in DIGEST_KEYS},
    }


def host_json(host: Host) -> int:
    doc = collect_host_json(host)
    run_dir = host.root / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp = run_dir / "host.json.tmp"
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(run_dir / "host.json")
    prune_stale_snapshots(host)
    for name, c in cast(dict[str, dict[str, Any]], doc["containers"]).items():
        if c.get("health") == "unhealthy":
            host.run(["docker", "restart", name], timeout=120)
            host.out(f"restarted unhealthy container {name}")
    return 0


# ------------------------------------------------------------ install helpers --


def lan_set_ports(ports: dict[str, Any], profiles: Iterable[str]) -> list[str]:
    """``proto:port`` lines install.sh adds to the nftables ``lan_tcp``/``lan_udp`` sets:
    enabled profiles' ports plus ``user`` (``base`` is literal in beepboop.nft)."""
    base = _entries(ports.get("base"))
    extra = allowed_ports(ports, profiles) - base
    return [f"{proto}:{port}" for proto, port in sorted(extra)]


def models(host: Host, action: str) -> int:
    """``models verify | update``: run the manifest tool from the audio image (OPERATIONS §13).

    ``verify`` runs with no network; ``update`` gets the host network for the download
    and re-verifies every sha256. Speaker-model swaps put voiceprints into re-enrolment
    (core notices the changed ``model_id`` at its next start).
    """
    env = read_env(host.env_file)
    image = f"ghcr.io/jaminb/beepboop-audio@{env.get('BB_AUDIO_DIGEST', '')}"
    network = "host" if action == "update" else "none"
    sub = "fetch" if action == "update" else "verify"
    r = host.run(
        ["docker", "run", "--rm", f"--network={network}", "-v",
         f"{host.root / 'models'}:/models", image, "python", "-m", "beepboop_bench.models",
         sub, "--root", "/models"],
        timeout=3600,
    )  # fmt: skip
    host.out(r.stdout.strip() or r.stderr.strip())
    return 0 if r.ok else 1


# ---------------------------------------------------------------------- logs --


def logs(host: Host, turn: str | None) -> int:
    if turn is None:
        r = host.run(host.compose("logs", "--tail", "200", "--no-color"))
        host.out(r.stdout)
        return 0 if r.ok else 1
    r = host.run(host.compose("logs", "--no-color", "--no-log-prefix", "core", "audio"))
    lines: list[tuple[str, str]] = []
    for line in r.stdout.splitlines():
        try:
            event = cast(dict[str, Any], json.loads(line))
        except ValueError:
            continue
        if event.get("turn_id") == turn:
            lines.append((str(event.get("timestamp", "")), line))
    for _, line in sorted(lines):
        host.out(line)
    return 0 if lines else 1


# ----------------------------------------------------------------------- main --

HOST_COMMANDS = ("doctor", "update", "db restore", "logs", "host-json")


def main(argv: Sequence[str] | None = None, host: Host | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    h = host or Host()
    if args[:2] == ["db", "restore"]:
        p = argparse.ArgumentParser(prog="beepboop db restore")
        p.add_argument("bundle", type=Path)
        p.add_argument("--identity", help="file holding the recovery key; default: prompt")
        ns = p.parse_args(args[2:])
        if ns.identity:
            identity = Path(ns.identity).read_text(encoding="utf-8")
        else:
            import getpass

            identity = getpass.getpass("Recovery key (AGE-SECRET-KEY-1...): ")
        return db_restore(h, ns.bundle, identity)
    if args[:1] == ["models"] and args[1:2] in (["verify"], ["update"]):
        return models(h, args[1])
    if args[:1] == ["lan-ports"]:  # install.sh: the nftables set elements to add
        env = read_env(h.env_file)
        for line in lan_set_ports(load_ports(h.deploy / "ports.yaml"), enabled_profiles(env)):
            h.out(line)
        return 0
    if not args or args[0] not in {"doctor", "update", "logs", "host-json"}:
        compose = h.compose("exec", "core", "beepboop", *args)
        os.execvp(compose[0], compose)  # noqa: S606 - forwarding is the point
    cmd, rest = args[0], args[1:]
    p = argparse.ArgumentParser(prog=f"beepboop {cmd}")
    if cmd == "doctor":
        p.add_argument("--preflight", action="store_true")
        ns = p.parse_args(rest)
        return doctor(h, with_preflight=ns.preflight)
    if cmd == "update":
        p.add_argument("--to", help="release name (default: the channel's latest)")
        p.add_argument("--channel", default="stable")
        p.add_argument("--manifest", help="release manifest file or https URL")
        ns = p.parse_args(rest)
        source = ns.manifest or RELEASES_URL.format(channel=ns.to if ns.to else ns.channel)
        try:
            manifest = load_manifest(source, h.http)
        except UpdateError as exc:
            h.out(f"update: {exc}")
            return 1
        return update(h, manifest=manifest)
    if cmd == "logs":
        p.add_argument("--turn")
        ns = p.parse_args(rest)
        return logs(h, ns.turn)
    return host_json(h)


if __name__ == "__main__":
    sys.exit(main())
