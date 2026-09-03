#!/usr/bin/env bash
# Installs the daily run. Run this INSIDE the container (or on any Debian or
# Ubuntu machine), as root.
#
#   ./deploy/proxmox/install.sh              # Docker, the recommended way
#   ./deploy/proxmox/install.sh --native     # no Docker: Edge, a venv, a timer
#
# Docker needs an LXC with nesting enabled; create-lxc.sh does that. If nesting
# is not an option on your node, --native installs Edge and the driver directly
# and schedules the run with a systemd timer instead.
set -euo pipefail

MODE="docker"
REPO_DIR="${REPO_DIR:-/opt/rewards-farmer}"
SERVICE_USER="${SERVICE_USER:-rewards}"

for argument in "$@"; do
	case "$argument" in
		--native) MODE="native" ;;
		--docker) MODE="docker" ;;
		*) echo "unknown option: $argument" >&2; exit 1 ;;
	esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }

# The checkout this script is part of, so running it from a clone in some other
# directory installs that clone rather than looking for one at REPO_DIR.
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "==> Installing from $SOURCE_DIR in $MODE mode"

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg tzdata

install_docker() {
	if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
		echo "==> Docker is already installed"

		return
	fi

	echo "==> Installing Docker from Docker's own repository"

	install -m 0755 -d /etc/apt/keyrings
	curl -fsSL https://download.docker.com/linux/debian/gpg \
		| gpg --dearmor -o /etc/apt/keyrings/docker.gpg
	chmod a+r /etc/apt/keyrings/docker.gpg

	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
		> /etc/apt/sources.list.d/docker.list

	apt-get update
	apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

	systemctl enable --now docker
}

setup_docker() {
	install_docker

	if [ "$SOURCE_DIR" != "$REPO_DIR" ]; then
		echo "==> Copying the checkout to $REPO_DIR"

		mkdir -p "$REPO_DIR"
		cp -a "$SOURCE_DIR/." "$REPO_DIR/"
	fi

	cd "$REPO_DIR"

	# The container runs as uid 1000 and bind mounts these, so they have to
	# exist and be owned by that uid before the first start. Created by Docker
	# otherwise, as root, and then nothing inside can write to them.
	mkdir -p data-dir state
	chown -R 1000:1000 data-dir state

	if [ ! -f .env ]; then
		cp .env.example .env

		# The zone the machine is already set to beats the example's guess.
		if [ -f /etc/timezone ]; then
			zone="$(cat /etc/timezone)"

			sed -i "s|^REWARDS_TZ=.*|REWARDS_TZ=${zone}|" .env
		fi

		echo "==> Wrote .env; edit it to change the time of the daily run"
	fi

	echo "==> Building the image (this pulls Edge, so it takes a few minutes)"

	docker compose build

	cat <<EOF

==> Installed. Two things left, in this order:

1. Sign the profile in. There is no screen on this machine, so the browser
   comes over VNC:

     cd $REPO_DIR
     docker compose run --rm --service-ports signin

   The port is published on this machine's loopback only, so from your desktop:

     ssh -N -L 5900:127.0.0.1:5900 root@$(hostname -I | awk '{print $1}')

   then point a VNC client at 127.0.0.1:5900, sign in on rewards.bing.com and
   on bing.com, and close the browser window.

2. Start the daily run:

     docker compose up -d
     docker compose logs -f

   It runs once a day at the time in .env, catches up a day it missed while
   this machine was off, and comes back after a reboot.

     docker compose run --rm rewards-farmer status   # what the last run did
     docker compose run --rm rewards-farmer once     # a run right now
EOF
}

setup_native() {
	echo "==> Installing Edge, the matching driver and a virtualenv"

	curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
		| gpg --dearmor -o /usr/share/keyrings/microsoft.gpg
	echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] \
https://packages.microsoft.com/repos/edge stable main" \
		> /etc/apt/sources.list.d/microsoft-edge.list

	apt-get update
	apt-get install -y --no-install-recommends \
		microsoft-edge-stable unzip fonts-liberation \
		python3 python3-venv python3-pip \
		xvfb x11vnc fluxbox

	# The driver has to match the browser build. "latest" drifts apart from the
	# installed Edge between releases and then refuses to start a session.
	edge_version="$(microsoft-edge --version | awk '{print $3}')"

	echo "==> Edge $edge_version, fetching the matching msedgedriver"

	curl -fsSL -o /tmp/edgedriver.zip \
		"https://msedgedriver.microsoft.com/${edge_version}/edgedriver_linux64.zip"
	unzip -oj /tmp/edgedriver.zip msedgedriver -d /usr/local/bin
	chmod +x /usr/local/bin/msedgedriver
	rm /tmp/edgedriver.zip

	if [ "$SOURCE_DIR" != "$REPO_DIR" ]; then
		mkdir -p "$REPO_DIR"
		cp -a "$SOURCE_DIR/." "$REPO_DIR/"
	fi

	id -u "$SERVICE_USER" >/dev/null 2>&1 || \
		useradd --system --home-dir /var/lib/rewards-farmer --shell /usr/sbin/nologin "$SERVICE_USER"

	python3 -m venv "$REPO_DIR/.venv"
	"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
	"$REPO_DIR/.venv/bin/pip" install --quiet "selenium>=4.46.0,<5.0.0" numpy

	# /opt stays read-only to the service; everything it writes is in
	# /var/lib/rewards-farmer, which the unit creates.
	mkdir -p /var/lib/rewards-farmer/data-dir /var/lib/rewards-farmer/state
	chown -R "$SERVICE_USER:$SERVICE_USER" /var/lib/rewards-farmer

	install -m 0644 "$REPO_DIR/deploy/systemd/rewards-farmer.service" /etc/systemd/system/
	install -m 0644 "$REPO_DIR/deploy/systemd/rewards-farmer.timer" /etc/systemd/system/

	[ -f /etc/rewards-farmer.env ] || \
		install -m 0644 "$REPO_DIR/deploy/systemd/rewards-farmer.env.example" /etc/rewards-farmer.env

	systemctl daemon-reload
	systemctl enable --now rewards-farmer.timer

	cat <<EOF

==> Installed. The timer is enabled; the profile still has to be signed in.

   There is no screen here, so run a browser over VNC once, as the service user
   and against the directory the timer's runs use. The script is the same one
   the container uses, and works outside it just as well:

     sudo -u $SERVICE_USER env \\
       HOME=/var/lib/rewards-farmer \\
       REWARDS_DATA_DIR=/var/lib/rewards-farmer/data-dir \\
       $REPO_DIR/docker/signin.sh

   Then, from your desktop:

     ssh -N -L 5900:127.0.0.1:5900 root@$(hostname -I | awk '{print $1}')

   point a VNC client at 127.0.0.1:5900, sign in on rewards.bing.com and on
   bing.com, and close the browser.

     systemctl list-timers rewards-farmer.timer   # when it next runs
     journalctl -u rewards-farmer.service -f      # what it did
     systemctl start rewards-farmer.service       # a run right now

   The time of day lives in the timer: sudo systemctl edit rewards-farmer.timer
EOF
}

case "$MODE" in
	docker) setup_docker ;;
	native) setup_native ;;
esac
