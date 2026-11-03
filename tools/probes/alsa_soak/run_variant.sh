#!/usr/bin/env sh
# V4 variant: the same 24 h ALSA soak with concurrent SSD write load and a `docker pull` in
# progress (ROADMAP V4 "Variant"). Run on the Pi HOST (needs docker and the SSD mount).
#
#   run_variant.sh [-D hw:1] [-d 86400] [-p /srv/beepboop/soak.tmp] [-i ghcr.io/home-assistant/home-assistant:stable]
#
# The write load is a loop of 256 MB `dd` files with fsync, deleted as it goes (stays under
# 1 GB on disk); the pull is repeated every 30 min with the image removed in between so it
# really downloads. Both stop when the soak ends.

set -u
DEVICE="hw:1"
DURATION=86400
SCRATCH="/srv/beepboop/soak.tmp"
IMAGE="ghcr.io/home-assistant/home-assistant:stable"
while getopts "D:d:p:i:" opt; do
  case "$opt" in
    D) DEVICE="$OPTARG" ;;
    d) DURATION="$OPTARG" ;;
    p) SCRATCH="$OPTARG" ;;
    i) IMAGE="$OPTARG" ;;
    *) echo "usage: $0 [-D device] [-d seconds] [-p scratch_path] [-i image]" >&2; exit 2 ;;
  esac
done
HERE="$(cd "$(dirname "$0")" && pwd)"

( i=0
  while :; do
    dd if=/dev/zero of="$SCRATCH.$i" bs=1M count=256 conv=fsync status=none 2>/dev/null
    rm -f "$SCRATCH.$i"
    i=$(( (i + 1) % 4 ))
  done ) &
WRITER=$!
( while :; do
    docker image rm -f "$IMAGE" >/dev/null 2>&1
    docker pull "$IMAGE" >/dev/null 2>&1
    sleep 1800
  done ) &
PULLER=$!
trap 'kill $WRITER $PULLER 2>/dev/null; rm -f "$SCRATCH".*' EXIT INT TERM

sh "$HERE/alsa_soak.sh" -D "$DEVICE" -d "$DURATION" -v "ssd-write+docker-pull"
