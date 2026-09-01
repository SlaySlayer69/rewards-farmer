#!/usr/bin/env bash
# Creates the container this bot runs in. Run this ON THE PROXMOX HOST, as root.
#
#   ./deploy/proxmox/create-lxc.sh
#   CTID=151 MEMORY=4096 ./deploy/proxmox/create-lxc.sh
#   DRY_RUN=1 ./deploy/proxmox/create-lxc.sh      # print the commands, run none
#
# Everything is an environment variable with a default: CTID, HOSTNAME_,
# STORAGE, TEMPLATE_STORAGE, TEMPLATE_NAME, BRIDGE, MEMORY, SWAP, CORES, DISK,
# UNPRIVILEGED, START. `pveam available` lists the template names your node
# knows, if the pinned default has aged out.
#
# It creates an unprivileged Debian container with nesting enabled, which is
# what Docker needs to run inside it, and nothing else. Installing the bot is
# the next script, run inside the container:
#
#   pct exec <CTID> -- bash -c 'curl -fsSL <raw url of install.sh> | bash'
#
# or, without trusting a pipe from the internet, push the checkout in:
#
#   pct push <CTID> rewards-farmer.tar /root/rewards-farmer.tar
set -euo pipefail

CTID="${CTID:-150}"
HOSTNAME_="${HOSTNAME_:-rewards-farmer}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"

# Edge is the memory-hungry part. Two gigabytes is enough for a single account
# and is where a run starts failing in ways that look like Bing's fault: pages
# that half render, a browser that stops answering. Four is comfortable, and is
# what a multi-account setup wants.
MEMORY="${MEMORY:-4096}"
SWAP="${SWAP:-1024}"
CORES="${CORES:-2}"
# The image is around 2GB with Edge in it, and Chromium's profile grows.
DISK="${DISK:-16}"

TEMPLATE_NAME="${TEMPLATE_NAME:-debian-12-standard_12.7-1_amd64.tar.zst}"
UNPRIVILEGED="${UNPRIVILEGED:-1}"
START="${START:-1}"
DRY_RUN="${DRY_RUN:-0}"

run() {
	echo "+ $*"

	[ "$DRY_RUN" = "1" ] && return 0

	"$@"
}

command -v pct >/dev/null 2>&1 || {
	echo "pct not found. This script runs on the Proxmox host, not inside a container." >&2

	exit 1
}

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }

if pct status "$CTID" >/dev/null 2>&1; then
	echo "Container $CTID already exists. Pick another CTID, or remove that one first." >&2

	exit 1
fi

TEMPLATE="${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE_NAME}"

if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE_NAME"; then
	echo "Template $TEMPLATE_NAME is not downloaded yet, fetching it."

	run pveam update
	run pveam download "$TEMPLATE_STORAGE" "$TEMPLATE_NAME"
fi

# features:
#   nesting=1  Docker inside the container. Also what lets the container run
#              its own systemd cleanly.
#   keyctl=1   Docker's own requirement in an unprivileged container: without
#              it the daemon fails to start on a kernel keyring call.
run pct create "$CTID" "$TEMPLATE" \
	--hostname "$HOSTNAME_" \
	--cores "$CORES" \
	--memory "$MEMORY" \
	--swap "$SWAP" \
	--rootfs "${STORAGE}:${DISK}" \
	--net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
	--features "nesting=1,keyctl=1" \
	--unprivileged "$UNPRIVILEGED" \
	--onboot 1 \
	--description "MS Rewards farmer. Daily run, see /opt/rewards-farmer."

if [ "$START" = "1" ]; then
	run pct start "$CTID"
fi

cat <<EOF

Container $CTID ($HOSTNAME_) is ready.

Next, inside it:

  pct enter $CTID
  apt-get update && apt-get install -y git
  git clone https://github.com/User0332/rewards-farmer /opt/rewards-farmer
  /opt/rewards-farmer/deploy/proxmox/install.sh

Then sign the browser profile in once - a headless node has no screen, so the
install script explains how - and the daily run starts on its own.
EOF
