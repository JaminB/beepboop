"""Static checks for install.sh, the systemd units and the image Dockerfiles (no Pi needed).

They keep the installer in step with the files it installs and with OPERATIONS §2-§5;
whether it really reaches a spoken claim code is ROADMAP v0.1 #1, pending on the Pi.
"""

from __future__ import annotations

import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1]
INSTALL = (DEPLOY / "install.sh").read_text(encoding="utf-8")


def test_install_is_posix_sh_with_the_documented_flags() -> None:
    assert INSTALL.startswith("#!/bin/sh\n")
    assert "set -eu" in INSTALL
    for flag in ("--dry-run", "--ram-profile", "--data-root", "--tier", "--profiles", "--luks",
                 "--force"):  # fmt: skip
        assert f"{flag})" in INSTALL, flag
    assert "\r" not in INSTALL


def test_install_ships_every_unit_and_host_file() -> None:
    for unit in (DEPLOY / "systemd").iterdir():
        assert unit.name in INSTALL, unit.name
    shipped = (
        "host/beepboop_host.py",
        "bin/beepboop",
        "udev/99-beepboop-xvf3800.rules",
        "nftables/beepboop.nft",
        "compose.yaml",
        "caddy/Caddyfile",
        "ports.yaml",
    )
    for rel in shipped:
        assert (DEPLOY / rel).exists(), rel
        assert f"$SCRIPT_DIR/{rel}" in INSTALL, rel


def test_kek_passphrase_path_matches_compose_and_is_never_overwritten() -> None:
    compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    assert "KEK_FILE=/etc/beepboop/kek.passphrase" in INSTALL
    assert "/etc/beepboop/kek.passphrase:/run/secrets/kek_passphrase:ro" in compose
    assert 'if [ ! -f "$KEK_FILE" ]' in INSTALL
    assert "head -c 32 /dev/urandom | base64" in INSTALL


def test_tier0_root_is_name_constrained() -> None:
    assert "nameConstraints = critical,permitted;DNS:$BB_HOST" in INSTALL
    for net in ("10.0.0.0/255.0.0.0", "172.16.0.0/255.240.0.0", "192.168.0.0/255.255.0.0"):
        assert f"IP:{net}" in INSTALL


def test_host_timer_runs_every_ten_seconds() -> None:
    timer = (DEPLOY / "systemd" / "beepboop-host.timer").read_text(encoding="utf-8")
    service = (DEPLOY / "systemd" / "beepboop-host.service").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=10s" in timer
    assert "beepboop_host.py host-json" in service
    main = (DEPLOY / "systemd" / "beepboop.service").read_text(encoding="utf-8")
    assert "up -d --remove-orphans" in main


def test_dockerfiles_pin_every_base_image_by_digest() -> None:
    for f in (DEPLOY / "docker").glob("*.Dockerfile"):
        text = f.read_text(encoding="utf-8")
        images = re.findall(r"^ARG [A-Z_]+_IMAGE=(\S+)", text, re.M)
        assert images, f.name
        for image in images:
            assert re.search(r"@sha256:[0-9a-f]{64}$", image), (f.name, image)
        for line in re.findall(r"^FROM (\S+)", text, re.M):
            assert line.startswith("${"), (f.name, line)  # always a pinned ARG, never a tag


def test_core_image_carries_modules_spa_and_no_node_at_runtime() -> None:
    text = (DEPLOY / "docker" / "core.Dockerfile").read_text(encoding="utf-8")
    runtime = text.rsplit("FROM ${PYTHON_IMAGE}", 1)[1]
    assert "COPY modules/ /app/modules/" in runtime
    assert "COPY --from=ui /src/ui/build /app/spa" in runtime
    assert "node" not in runtime.lower().replace("no-new", "")
    assert "--frozen" in text


def test_audio_image_has_no_weights() -> None:
    text = (DEPLOY / "docker" / "audio.Dockerfile").read_text(encoding="utf-8")
    assert "models/" not in text.split("FROM ${PYTHON_IMAGE}\n", 2)[-1]
