"""The host CLI (deploy/host/beepboop_host.py) against a fake Docker, /proc and clock."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import beepboop_host as bh

DEPLOY = Path(__file__).resolve().parents[1]
D = {k: "sha256:" + c * 64 for k, c in zip(bh.DIGEST_KEYS, "abcde", strict=True)}
NEW = {k: "sha256:" + c * 64 for k, c in zip(bh.DIGEST_KEYS, "56789", strict=True)}


@dataclass
class FakeDocker:
    """Scripted answers by argv substring; records every call."""

    rules: list[tuple[str, Callable[[Sequence[str]], bh.Result]]] = field(
        default_factory=list[tuple[str, Callable[[Sequence[str]], bh.Result]]]
    )
    calls: list[str] = field(default_factory=list[str])

    def on(self, needle: str, result: bh.Result | Callable[[Sequence[str]], bh.Result]) -> None:
        if isinstance(result, bh.Result):
            fixed = result

            def fn(_argv: Sequence[str]) -> bh.Result:
                return fixed

            self.rules.insert(0, (needle, fn))
        else:
            self.rules.insert(0, (needle, result))

    def __call__(
        self, argv: Sequence[str], *, input_text: str | None = None, timeout: float | None = None
    ) -> bh.Result:
        line = " ".join(argv)
        self.calls.append(line)
        for needle, fn in self.rules:
            if needle in line:
                return fn(argv)
        return bh.Result(0)

    def ran(self, needle: str) -> bool:
        return any(needle in c for c in self.calls)


@dataclass
class Health:
    """What GET /api/health answers, settable mid-test."""

    status: str | None = "ok"
    schema: int = 1

    def __call__(self, url: str) -> dict[str, Any] | None:
        if self.status is None:
            return None
        return {"status": self.status, "schema_version": self.schema}


def _host(tmp_path: Path, docker: FakeDocker, health: Health) -> bh.Host:
    root = tmp_path / "srv"
    (root / "deploy").mkdir(parents=True)
    (root / "ha" / ".storage").mkdir(parents=True)
    (root / "core").mkdir()
    (root / "backups").mkdir()
    shutil.copy(DEPLOY / "ports.yaml", root / "deploy" / "ports.yaml")
    (root / "deploy" / "compose.yaml").write_text("name: beepboop\n")
    env = "# digests\n" + "".join(f"{k}={v}\n" for k, v in D.items()) + "BB_HOST=beepboop.local\n"
    (root / "deploy" / ".env").write_text(env)
    (root / "ha" / "configuration.yaml").write_text("old: true\n")
    (root / "ha" / ".storage" / "core.config").write_text("{}")
    (root / "ha" / "home-assistant_v2.db").write_bytes(b"history")
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    docker.on("chronyc tracking", bh.Result(0, "Leap status     : Normal\n"))
    docker.on("{{.State.Health.Status}}", bh.Result(0, "healthy\n"))
    lines: list[str] = []
    elapsed = [0.0]

    def fake_sleep(seconds: float) -> None:
        elapsed[0] += seconds

    def snapshot(argv: Sequence[str]) -> bh.Result:
        target = argv[-1].replace("/backups", str(root / "backups"))
        Path(target).write_bytes(b"SQLite snapshot")
        return bh.Result(0, "snapshot")

    docker.on("db backup --out", snapshot)
    return bh.Host(
        root=root,
        run=docker,
        http=health,
        clock=lambda: datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
        sleep=fake_sleep,
        monotonic=lambda: elapsed[0],
        proc=proc,
        sys_root=tmp_path / "sys",
        reboot_flag=tmp_path / "no-reboot",
        out=lines.append,
    )


# ---------------------------------------------------------------- doctor --

TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"


def _tcp(addr_hex: str, port: int, state: str = "0A") -> str:
    return f"   0: {addr_hex}:{port:04X} 00000000:0000 {state} 0:0 00:0 0 0\n"


def test_doctor_flags_an_undeclared_listening_port(tmp_path: Path) -> None:
    docker = FakeDocker()
    host = _host(tmp_path, docker, Health())
    docker.on("nft list table", bh.Result(0, "tcp dport 8123 drop"))
    (host.proc / "net" / "tcp").write_text(
        TCP_HEADER
        + _tcp("00000000", 443)  # declared (base)
        + _tcp("0100007F", 8080)  # loopback: ignored
        + _tcp("00000000", 1883)  # MQTT on the LAN, not declared
        + _tcp("00000000", 9999, state="01")  # established, not listening
    )
    (host.proc / "net" / "udp").write_text(TCP_HEADER + _tcp("00000000", 68, state="07"))
    (host.sys_root / "fs" / "cgroup").mkdir(parents=True)
    (host.sys_root / "fs" / "cgroup" / "cgroup.controllers").write_text("cpu memory io\n")
    lines: list[str] = []
    host.out = lines.append
    assert bh.doctor(host, with_preflight=False) == 1
    report = "\n".join(lines)
    assert "fail port 1883/tcp" in report
    assert "443" not in report
    assert "8080" not in report
    assert "9999" not in report
    assert "port 68" not in report  # DHCP client socket
    assert "ok   memory limits" in report
    assert "ok   firewall" in report


def test_doctor_fails_when_ha_binds_the_lan(tmp_path: Path) -> None:
    docker = FakeDocker()
    host = _host(tmp_path, docker, Health())
    (host.proc / "net" / "tcp6").write_text(TCP_HEADER + _tcp("0" * 32, 8123))
    checks = bh.check_ports(
        bh.listening_sockets(host.proc), bh.load_ports(DEPLOY / "ports.yaml"), []
    )
    assert [c.detail for c in checks if c.status == "fail"] == [
        "must be loopback-only but listens on ::"
    ]


def test_profile_and_user_ports_are_allowed() -> None:
    ports = bh.load_ports(DEPLOY / "ports.yaml")
    music = bh.Socket("tcp", "0.0.0.0", 8097)  # noqa: S104 - a parsed socket, not a bind
    assert bh.check_ports([music], ports, [])[0].status == "fail"
    assert bh.check_ports([music], ports, ["music"])[0].status == "ok"
    assert bh.enabled_profiles({"COMPOSE_PROFILES": "music, "}) == ["music"]


def test_proc_net_address_decoding() -> None:
    assert bh._hex_addr("0100007F") == "127.0.0.1"  # pyright: ignore[reportPrivateUsage]
    assert bh._hex_addr("00000000000000000000000001000000") == "::1"  # pyright: ignore[reportPrivateUsage]
    v4_mapped = "0000000000000000FFFF00000100007F"
    assert bh._hex_addr(v4_mapped) == "127.0.0.1"  # pyright: ignore[reportPrivateUsage]


def test_cgroup_memory_check(tmp_path: Path) -> None:
    proc, sys_root = tmp_path / "proc", tmp_path / "sys"
    proc.mkdir()
    (proc / "cmdline").write_text("console=tty1 root=/dev/sda2")
    assert bh.check_cgroup_memory(proc, sys_root).status == "fail"
    (proc / "cmdline").write_text("cgroup_enable=memory cgroup_memory=1 swapaccount=1")
    assert bh.check_cgroup_memory(proc, sys_root).status == "warn"


def test_preflight_on_a_wrong_machine(tmp_path: Path) -> None:
    docker = FakeDocker()
    host = _host(tmp_path, docker, Health())
    docker.on("uname -m", bh.Result(0, "x86_64\n"))
    docker.on("get_throttled", bh.Result(0, "throttled=0x50005\n"))
    docker.on("findmnt", bh.Result(0, "/dev/mmcblk0p2\n"))
    status = {c.name: c.status for c in bh.preflight(host, docker)}
    assert status["architecture"] == "fail"
    assert status["throttling"] == "fail"
    assert status["root on SSD"] == "fail"
    assert status["microphone array"] == "fail"


# ---------------------------------------------------------------- update --


def _manifest(digests: dict[str, str] = NEW, schema: int | None = None) -> bh.ReleaseManifest:
    data: dict[str, Any] = {"release": "0.1.1", "digests": digests}
    if schema is not None:
        data["schema_version"] = schema
    return bh.ReleaseManifest.parse(data)


def test_manifest_validation(tmp_path: Path) -> None:
    with pytest.raises(bh.UpdateError, match="bad digest"):
        bh.ReleaseManifest.parse({"digests": {"BB_CORE_DIGEST": "latest"}})
    with pytest.raises(bh.UpdateError, match="missing"):
        bh.ReleaseManifest.parse({"digests": {"BB_CORE_DIGEST": NEW["BB_CORE_DIGEST"]}})
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"release": "0.1.1", "digests": NEW}))
    assert bh.load_manifest(str(f), lambda _u: None).release == "0.1.1"


def test_update_happy_path(tmp_path: Path) -> None:
    docker, health = FakeDocker(), Health()
    host = _host(tmp_path, docker, health)
    assert bh.update(host, manifest=_manifest()) == 0
    env = bh.read_env(host.env_file)
    assert all(env[k] == v for k, v in NEW.items())
    assert env["BB_HOST"] == "beepboop.local"  # untouched lines survive
    assert docker.ran("--env-file")
    assert docker.ran("pull")
    assert docker.ran("up -d --remove-orphans")
    assert docker.ran("chat --as guest --dry-run what time is it")
    snap = host.root / "backups" / "pre-update-20261104T120000Z"
    assert (snap / "beepboop.db").exists()
    assert (snap / ".env").exists()
    assert (snap / "ha" / "configuration.yaml").exists()  # HA digest changed
    assert not (host.root / "ha" / "home-assistant_v2.db").exists()  # moved aside
    status = json.loads((host.root / "run" / "update.json").read_text())
    assert status["ok"] is True
    assert status["release"] == "0.1.1"


def test_update_to_a_broken_image_rolls_back_and_says_so(tmp_path: Path) -> None:
    """ROADMAP v0.1 #9 with fakes: verify fails, the previous .env, DB and HA come back."""
    docker, health = FakeDocker(), Health(schema=1)
    host = _host(tmp_path, docker, health)
    ups = 0

    def up(_argv: Sequence[str]) -> bh.Result:
        nonlocal ups
        ups += 1
        # The broken release migrates the DB to 2 and never answers health; the rollback works.
        if ups == 1:
            health.status, health.schema = None, 2
            (host.root / "core" / "beepboop.db").write_bytes(b"migrated to v2")
            (host.root / "ha" / "configuration.yaml").write_text("broken: true\n")
        else:
            health.status, health.schema = "ok", 1
        return bh.Result(0)

    docker.on("up -d", up)
    lines: list[str] = []
    host.out = lines.append
    assert bh.update(host, manifest=_manifest()) == 1
    env = bh.read_env(host.env_file)
    assert all(env[k] == v for k, v in D.items())
    assert (host.root / "core" / "beepboop.db").read_bytes() == b"SQLite snapshot"
    assert (host.root / "ha" / "configuration.yaml").read_text() == "old: true\n"
    assert "rollback: the update failed and I rolled back" in lines
    status = json.loads((host.root / "run" / "update.json").read_text())
    assert status == status | {"ok": False, "message": "the update failed and I rolled back"}


def test_update_refuses_without_disk_or_clock(tmp_path: Path) -> None:
    docker = FakeDocker()
    host = _host(tmp_path, docker, Health())
    docker.on("chronyc tracking", bh.Result(0, "Leap status     : Not synchronised\n"))
    assert bh.update(host, manifest=_manifest()) == 1
    assert not docker.ran("pull")
    host.reboot_flag.write_text("")
    assert bh.update(host, manifest=_manifest()) == 1


def test_failed_pull_changes_nothing(tmp_path: Path) -> None:
    docker = FakeDocker()
    host = _host(tmp_path, docker, Health())
    docker.on("pull", bh.Result(1, "", "manifest unknown"))
    assert bh.update(host, manifest=_manifest()) == 1
    assert bh.read_env(host.env_file)["BB_CORE_DIGEST"] == D["BB_CORE_DIGEST"]
    assert not docker.ran("up -d")
    assert not (host.deploy / ".env.next").exists()


def test_downgrade_across_a_migration_restores_the_older_snapshot(tmp_path: Path) -> None:
    docker, health = FakeDocker(), Health(schema=2)
    host = _host(tmp_path, docker, health)
    old = host.root / "backups" / "pre-update-20261001T000000Z"
    old.mkdir()
    (old / "beepboop.db").write_bytes(b"db at schema 1")
    (old / "schema_version").write_text("1")

    def up(_argv: Sequence[str]) -> bh.Result:
        health.schema = 1
        return bh.Result(0)

    docker.on("up -d", up)
    assert bh.update(host, manifest=_manifest(schema=1)) == 0
    assert (host.root / "core" / "beepboop.db").read_bytes() == b"db at schema 1"
    # Without a snapshot at the target schema, the downgrade is refused up front.
    shutil.rmtree(old, ignore_errors=True)  # the successful update already pruned it
    health.schema = 3
    host2 = _host(tmp_path / "b", FakeDocker(), health)
    assert bh.update(host2, manifest=_manifest(schema=1)) == 1


# ------------------------------------------------------- restore, host.json --


def test_db_restore_runs_core_in_a_one_off_container(tmp_path: Path) -> None:
    docker = FakeDocker()
    host = _host(tmp_path, docker, Health())
    bundle = tmp_path / "beepboop-beepboop.local-20261103T033000Z.tar.age"
    bundle.write_bytes(b"age-encryption.org/v1 ...")
    assert bh.db_restore(host, bundle, "AGE-SECRET-KEY-1XYZ") == 0
    order = [c for c in docker.calls if "compose" in c]
    assert "stop core homeassistant" in order[0]
    assert "run --rm --no-deps -T core beepboop db restore /backups/restore/" in order[1]
    assert "up -d" in order[2]
    docker.on("db restore", bh.Result(2, "", "restore refused: wrong key"))
    assert bh.db_restore(host, bundle, "AGE-SECRET-KEY-1BAD") == 1


def test_host_json_and_unhealthy_restart(tmp_path: Path) -> None:
    docker = FakeDocker()
    host = _host(tmp_path, docker, Health())
    docker.on(
        "docker ps -a",
        bh.Result(0, json.dumps({"Names": "beepboop-audio", "State": "running",
                                 "Status": "Up 2 hours (unhealthy)"}) + "\n"),
    )  # fmt: skip
    docker.on(
        "docker stats",
        bh.Result(0, json.dumps({"Name": "beepboop-audio", "MemUsage": "512MiB / 768MiB",
                                 "CPUPerc": "41.5%"}) + "\n"),
    )  # fmt: skip
    docker.on("get_throttled", bh.Result(0, "throttled=0x50000\n"))
    docker.on("chronyc -c tracking", bh.Result(0, "A,B,C,D,0.000123,F\n"))
    assert bh.host_json(host) == 0
    doc = json.loads((host.root / "run" / "host.json").read_text())
    audio = doc["containers"]["beepboop-audio"]
    assert audio == audio | {"health": "unhealthy", "mem_mb": 512.0, "mem_limit_mb": 768.0}
    assert doc["throttled"] == "0x50000"
    assert doc["chrony_offset_s"] == pytest.approx(0.000123)
    assert docker.ran("docker restart beepboop-audio")
    assert doc["release"]["BB_CORE_DIGEST"] == D["BB_CORE_DIGEST"]


def test_logs_for_one_turn(tmp_path: Path) -> None:
    docker = FakeDocker()
    host = _host(tmp_path, docker, Health())
    stream = "\n".join(
        json.dumps(e)
        for e in [
            {"timestamp": "2", "event": "tts.first_chunk", "turn_id": "T1"},
            {"timestamp": "1", "event": "stt.final", "turn_id": "T1"},
            {"timestamp": "1", "event": "x", "turn_id": "T2"},
        ]
    )
    docker.on("logs --no-color --no-log-prefix", bh.Result(0, stream + "\nnot json\n"))
    lines: list[str] = []
    host.out = lines.append
    assert bh.logs(host, "T1") == 0
    assert [json.loads(x)["event"] for x in lines] == ["stt.final", "tts.first_chunk"]


def test_write_env_keeps_comments_and_adds_missing(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("# c\nA=1\nB=2\n")
    bh.write_env(f, {"B": "3", "C": "4"})
    assert f.read_text() == "# c\nA=1\nB=3\nC=4\n"


def test_lan_set_ports_excludes_base() -> None:
    ports = bh.load_ports(DEPLOY / "ports.yaml")
    assert bh.lan_set_ports(ports, []) == []
    assert bh.lan_set_ports(ports, ["music"]) == ["tcp:8097"]


def test_models_verify_has_no_network(tmp_path: Path) -> None:
    docker = FakeDocker()
    host = _host(tmp_path, docker, Health())
    assert bh.models(host, "verify") == 0
    assert docker.ran("--network=none")
    assert docker.ran(f"beepboop-audio@{D['BB_AUDIO_DIGEST']}")
    assert bh.models(host, "update") == 0
    assert docker.ran("--network=host")
