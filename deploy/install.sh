#!/bin/sh
# beepboop installer (docs/OPERATIONS.md §5 is the spec; §2 lists what it does to the OS).
#
# Read it first, then run it from a release checkout as root:
#   sudo sh deploy/install.sh [--dry-run] [--ram-profile 4g|8g] [--data-root PATH]
#                             [--tier 0|1] [--profiles music] [--luks] [--force]
#
# It is idempotent: every step checks before it changes anything, and prints what it is
# about to do. --dry-run prints the steps and changes nothing. Release digests come from
# deploy/release.env (written by the release pipeline) or an existing /srv/beepboop/deploy/.env.
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname -- "$SCRIPT_DIR")
ROOT=/srv/beepboop
KEK_FILE=/etc/beepboop/kek.passphrase
DRY_RUN=0
FORCE=0
LUKS=0
RAM_PROFILE=""
DATA_ROOT=""
TIER=0
PROFILES=""
HEALTH_WAIT_S=600

usage() {
	sed -n '2,10p' "$0"
	exit 2
}

while [ $# -gt 0 ]; do
	case "$1" in
	--dry-run) DRY_RUN=1 ;;
	--force) FORCE=1 ;;
	--luks) LUKS=1 ;;
	--ram-profile)
		RAM_PROFILE=${2:?}
		shift
		;;
	--data-root)
		DATA_ROOT=${2:?}
		shift
		;;
	--tier)
		TIER=${2:?}
		shift
		;;
	--profiles)
		PROFILES=${2:?}
		shift
		;;
	-h | --help) usage ;;
	*)
		echo "unknown option: $1" >&2
		usage
		;;
	esac
	shift
done

step() { printf '\n==> %s\n' "$*"; }
say() { printf '    %s\n' "$*"; }
die() {
	printf 'install.sh: %s\n' "$*" >&2
	exit 1
}

# run CMD...: print, then execute unless --dry-run.
run() {
	printf '    $ %s\n' "$*"
	if [ "$DRY_RUN" -eq 0 ]; then
		"$@"
	fi
}

# write_file PATH MODE: stdin to PATH atomically, only if the content changed.
write_file() {
	_tmp=$(mktemp)
	cat >"$_tmp"
	if [ -f "$1" ] && cmp -s "$_tmp" "$1"; then
		rm -f "$_tmp"
		return 0
	fi
	printf '    write %s\n' "$1"
	if [ "$DRY_RUN" -eq 0 ]; then
		mkdir -p "$(dirname -- "$1")"
		install -m "$2" "$_tmp" "$1"
	fi
	rm -f "$_tmp"
}

# t CMD...: 0 if CMD succeeds else 1, without tripping set -e.
t() {
	if "$@"; then echo 0; else echo 1; fi
}

# check NAME STATUS DETAIL: preflight bookkeeping (--force downgrades).
FAILED=0
check() {
	if [ "$2" -eq 0 ]; then
		say "ok   $1: $3"
	elif [ "$FORCE" -eq 1 ] && [ "$1" != "architecture" ]; then
		say "warn $1: $3 (--force)"
	else
		say "FAIL $1: $3"
		FAILED=1
	fi
}

[ "$(id -u)" -eq 0 ] || [ "$DRY_RUN" -eq 1 ] || die "run as root (sudo sh deploy/install.sh)"
case "$TIER" in 0 | 1) ;; *) die "--tier must be 0 or 1" ;; esac
[ "$TIER" -eq 0 ] || die "Tier 1 HTTPS is v0.2 (docs/ROADMAP.md); use --tier 0"
case "$PROFILES" in "" | music) ;; *) die "the only v0.1 profile is music" ;; esac

# ------------------------------------------------------------------ 1 preflight --
step "1/7 preflight (docs/OPERATIONS.md §5)"
ARCH=$(uname -m)
check architecture "$(t [ "$ARCH" = aarch64 ])" "$ARCH (64-bit Raspberry Pi OS required)"
MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)
case "$MODEL" in *"Raspberry Pi 4"*) r=0 ;; *) r=1 ;; esac
check board $r "$MODEL"
MEM_KB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
check memory "$(t [ "$MEM_KB" -ge 3565158 ])" "$((MEM_KB / 1024)) MiB"
ROOT_SRC=$(findmnt -n -o SOURCE / || echo unknown)
case "$ROOT_SRC" in *mmcblk0*) r=1 ;; *) r=0 ;; esac
check "root on SSD" $r "$ROOT_SRC (boot from a USB SSD, not the SD card)"
FREE_KB=$(df -Pk / | awk 'NR==2 {print $4}')
check "disk free" "$(t [ "$FREE_KB" -ge 2929688 ])" "$((FREE_KB / 1024)) MiB"
THROTTLED=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || echo unknown)
check throttling "$(t [ "$THROTTLED" = 0x0 ])" "$THROTTLED (power supply and cooling, OPERATIONS §1)"
TEMP=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 99000)
check temperature "$(t [ "$TEMP" -lt 70000 ])" "$((TEMP / 1000)) C"
check "microphone array" "$(t grep -q XVF3800 /proc/asound/cards)" "ReSpeaker XVF3800 on USB"
if [ "$FAILED" -eq 1 ]; then
	[ "$DRY_RUN" -eq 1 ] || die "preflight failed; see docs/OPERATIONS.md §1-§2 (or --force)"
	say "(dry run: continuing past failed checks)"
fi
if [ -z "$RAM_PROFILE" ]; then
	if [ "$MEM_KB" -ge 7000000 ]; then RAM_PROFILE=8g; else RAM_PROFILE=4g; fi
fi
if [ "$RAM_PROFILE" = 4g ] && [ -n "$PROFILES" ] && [ "$PROFILES" != music ]; then
	say "warn a 4 GB Pi supports at most the music profile (OPERATIONS §1)"
fi
say "RAM profile: $RAM_PROFILE"

# ------------------------------------------------------ 2 host packages, kernel --
step "2/7 host packages and kernel"
export DEBIAN_FRONTEND=noninteractive
if ! command -v docker >/dev/null 2>&1; then
	run install -m 0755 -d /etc/apt/keyrings
	run sh -c 'curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc'
	# shellcheck disable=SC1091 # the OS release file only exists on the target
	CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
	echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $CODENAME stable" |
		write_file /etc/apt/sources.list.d/docker.list 0644
	run apt-get update
	run apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
# Pre-pull in the background while the rest of apt runs (needs the digests, step 3 checks).
RELEASE_ENV="$SCRIPT_DIR/release.env"
PULL_PID=""
if [ -f "$RELEASE_ENV" ] && [ "$DRY_RUN" -eq 0 ]; then
	docker compose -f "$SCRIPT_DIR/compose.yaml" --env-file "$RELEASE_ENV" pull -q >/dev/null 2>&1 &
	PULL_PID=$!
fi
run apt-get install -y zram-tools nftables rpi-eeprom avahi-daemon chrony fake-hwclock \
	unattended-upgrades python3 python3-yaml openssl
printf 'ALGO=zstd\nPERCENT=50\n' | write_file /etc/default/zramswap 0644
printf 'vm.swappiness=60\n' | write_file /etc/sysctl.d/90-beepboop.conf 0644
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\n' |
	write_file /etc/systemd/journald.conf.d/90-beepboop.conf 0644
printf '{\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "20m", "max-file": "5"}\n}\n' |
	write_file /etc/docker/daemon.json 0644
printf 'makestep 1 -1\n' | write_file /etc/chrony/conf.d/90-beepboop.conf 0644
printf '[Manager]\nRuntimeWatchdogSec=30\n' | write_file /etc/systemd/system.conf.d/90-beepboop.conf 0644
# shellcheck disable=SC2016 # ${distro_codename} is expanded by unattended-upgrades, not sh
printf 'Unattended-Upgrade::Origins-Pattern { "origin=Debian,codename=${distro_codename},label=Debian-Security"; };\nUnattended-Upgrade::Automatic-Reboot "false";\n' |
	write_file /etc/apt/apt.conf.d/52beepboop-unattended 0644
CMDLINE=/boot/firmware/cmdline.txt
REBOOT_NEEDED=0
if [ -f "$CMDLINE" ] && ! grep -q 'cgroup_memory=1' "$CMDLINE"; then
	say "append cgroup flags to $CMDLINE (reboot needed once)"
	run sed -i '1 s/$/ cgroup_enable=memory cgroup_memory=1 swapaccount=1/' "$CMDLINE"
	REBOOT_NEEDED=1
fi
run systemctl enable --now zramswap chrony avahi-daemon nftables docker
run sysctl --system

# --------------------------------------------------------------------- 3 data root --
step "3/7 data root $ROOT"
if [ "$LUKS" -eq 1 ]; then
	# The encrypted volume is prepared by the owner (cryptsetup luksFormat + open), because
	# its passphrase is typed over SSH at every boot (OPERATIONS §2 step 10, SECURITY.md).
	SRC=$(findmnt -n -o SOURCE "${DATA_ROOT:-$ROOT}" 2>/dev/null || true)
	case "$SRC" in /dev/mapper/*) say "data root is on $SRC" ;; *) die "--luks: mount an opened LUKS volume at ${DATA_ROOT:-$ROOT} first" ;; esac
fi
if [ -n "$DATA_ROOT" ] && [ "$DATA_ROOT" != "$ROOT" ]; then
	run mkdir -p "$DATA_ROOT"
	[ -e "$ROOT" ] || run ln -s "$DATA_ROOT" "$ROOT"
fi
for d in deploy/caddy caddy/data/caddy/pki/authorities/local caddy/config config core backups ha models run music; do
	[ -d "$ROOT/$d" ] || run mkdir -p "$ROOT/$d"
done
run chmod 0700 "$ROOT/core" "$ROOT/backups"
if [ ! -f "$KEK_FILE" ]; then
	say "generate the KEK passphrase file $KEK_FILE (never overwritten, never backed up)"
	if [ "$DRY_RUN" -eq 0 ]; then
		install -d -m 0700 /etc/beepboop
		umask 077
		head -c 32 /dev/urandom | base64 >"$KEK_FILE"
		chmod 0400 "$KEK_FILE"
	fi
fi
write_file "$ROOT/deploy/compose.yaml" 0644 <"$SCRIPT_DIR/compose.yaml"
write_file "$ROOT/deploy/caddy/Caddyfile" 0644 <"$SCRIPT_DIR/caddy/Caddyfile"
write_file "$ROOT/deploy/ports.yaml" 0644 <"$SCRIPT_DIR/ports.yaml"
ENV_FILE="$ROOT/deploy/.env"
if [ ! -f "$ENV_FILE" ]; then
	if [ "$RAM_PROFILE" = 8g ]; then
		MEMS='s/^BB_MEM_CORE=.*/BB_MEM_CORE=1g/;s/^BB_MEM_AUDIO=.*/BB_MEM_AUDIO=1g/;s/^BB_MEM_HA=.*/BB_MEM_HA=2g/;s/^BB_MEM_MUSIC=.*/BB_MEM_MUSIC=768m/'
	else
		MEMS='s/^$//'
	fi
	sed -e "$MEMS" -e "s/^BB_TIER=.*/BB_TIER=$TIER/" -e "s/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=$PROFILES/" \
		"$SCRIPT_DIR/.env.example" | write_file "$ENV_FILE" 0644
	if [ -f "$RELEASE_ENV" ] && [ "$DRY_RUN" -eq 0 ]; then
		grep '^BB_[A-Z]*_DIGEST=sha256:' "$RELEASE_ENV" | while IFS='=' read -r key value; do
			sed -i "s|^$key=.*|$key=$value|" "$ENV_FILE"
		done
	fi
fi
if [ "$DRY_RUN" -eq 0 ] && grep -q '^BB_CORE_DIGEST=$' "$ENV_FILE"; then
	die "no image digests: run from a release checkout (deploy/release.env) or fill $ENV_FILE"
fi
BB_HOST=$(sed -n 's/^BB_HOST=//p' "$ENV_FILE" 2>/dev/null || echo beepboop.local)
BB_HOST=${BB_HOST:-beepboop.local}
[ -f "$ROOT/config/beepboop.yaml" ] ||
	printf '# Household config read by core and audio (ARCHITECTURE §9). Provider selection:\nproviders: {}\n' |
	write_file "$ROOT/config/beepboop.yaml" 0644
if [ ! -f "$ROOT/ha/configuration.yaml" ]; then
	sed "s|https://beepboop.local|https://$BB_HOST|g" "$SCRIPT_DIR/homeassistant/configuration.yaml" |
		write_file "$ROOT/ha/configuration.yaml" 0644
fi

# Tier-0 root with nameConstraints (OPERATIONS §4): a leaked key can only mint names
# that point at the house. Generated here, not by Caddy; the key stays in Caddy's volume.
PKI="$ROOT/caddy/data/caddy/pki/authorities/local"
if [ ! -f "$PKI/root.crt" ]; then
	say "generate the name-constrained Tier-0 root CA for $BB_HOST"
	if [ "$DRY_RUN" -eq 0 ]; then
		EXT=$(mktemp)
		cat >"$EXT" <<EOT
[req]
distinguished_name = dn
prompt = no
[dn]
CN = beepboop local CA ($BB_HOST)
[ext]
basicConstraints = critical,CA:true
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
nameConstraints = critical,permitted;DNS:$BB_HOST,permitted;DNS:.local,permitted;IP:10.0.0.0/255.0.0.0,permitted;IP:172.16.0.0/255.240.0.0,permitted;IP:192.168.0.0/255.255.0.0,permitted;IP:fd00:0:0:0:0:0:0:0/ff00:0:0:0:0:0:0:0
EOT
		openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
			-keyout "$PKI/root.key" -out "$PKI/root.crt" -days 3650 \
			-config "$EXT" -extensions ext
		chmod 0600 "$PKI/root.key"
		rm -f "$EXT"
	fi
fi
write_file /etc/udev/rules.d/99-beepboop-xvf3800.rules 0644 <"$SCRIPT_DIR/udev/99-beepboop-xvf3800.rules"
run udevadm control --reload-rules

# Firewall: base ports are literal in the file; enabled profiles + user ports join the sets.
write_file /etc/nftables.d/beepboop.nft 0644 <"$SCRIPT_DIR/nftables/beepboop.nft"
grep -q 'nftables.d/beepboop.nft' /etc/nftables.conf 2>/dev/null ||
	run sh -c 'echo "include \"/etc/nftables.d/beepboop.nft\"" >> /etc/nftables.conf'
# Host CLI and timers.
write_file /usr/local/lib/beepboop/beepboop_host.py 0755 <"$SCRIPT_DIR/host/beepboop_host.py"
write_file /usr/local/bin/beepboop 0755 <"$SCRIPT_DIR/bin/beepboop"
for unit in beepboop.service beepboop-host.service beepboop-host.timer; do
	write_file "/etc/systemd/system/$unit" 0644 <"$SCRIPT_DIR/systemd/$unit"
done
run nft -f /etc/nftables.d/beepboop.nft
if [ "$DRY_RUN" -eq 0 ]; then
	python3 /usr/local/lib/beepboop/beepboop_host.py lan-ports | while IFS=: read -r proto port; do
		[ -n "$port" ] && nft add element inet beepboop "lan_$proto" "{ $port }"
	done
fi
run systemctl daemon-reload

# ------------------------------------------------------------------------- 4 models --
step "4/7 models (downloaded, never vendored; OPERATIONS §13)"
write_file "$ROOT/models/manifest.yaml" 0644 <"$REPO_DIR/models/manifest.yaml"
AUDIO_IMAGE="ghcr.io/jaminb/beepboop-audio@$(sed -n 's/^BB_AUDIO_DIGEST=//p' "$ENV_FILE")"
run docker run --rm --network=host -v "$ROOT/models:/models" "$AUDIO_IMAGE" \
	python -m beepboop_bench.models fetch --root /models
run docker run --rm --network=none -v "$ROOT/models:/models" "$AUDIO_IMAGE" \
	python -m beepboop_bench.models licenses --root /models

# ------------------------------------------------------------------------- 5 images --
step "5/7 images by digest"
[ -z "$PULL_PID" ] || wait "$PULL_PID" || true
run docker compose -f "$ROOT/deploy/compose.yaml" pull

# ----------------------------------------------------------------------------- 6 up --
step "6/7 start"
run systemctl enable --now beepboop.service beepboop-host.timer
if [ "$DRY_RUN" -eq 0 ]; then
	say "waiting for core and audio to be healthy (Home Assistant finishes in the background)"
	waited=0
	until [ "$(docker inspect -f '{{.State.Health.Status}}' beepboop-core 2>/dev/null)" = healthy ] &&
		[ "$(docker inspect -f '{{.State.Health.Status}}' beepboop-audio 2>/dev/null)" = healthy ]; do
		if [ "$waited" -ge "$HEALTH_WAIT_S" ]; then
			docker compose -f "$ROOT/deploy/compose.yaml" logs --tail 50 core audio
			die "core or audio did not become healthy"
		fi
		sleep 5
		waited=$((waited + 5))
	done
fi

# ------------------------------------------------------------------------ 7 hand-off --
step "7/7 done"
say "Listen for the claim code, then open https://$BB_HOST"
say "First install the device CA on your phone: http://$BB_HOST/ca"
[ "$REBOOT_NEEDED" -eq 0 ] || say "Reboot once so memory limits are enforced (beepboop doctor checks)."
