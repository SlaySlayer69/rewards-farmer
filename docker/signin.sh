#!/bin/sh
# A browser window on a machine with no screen, so the profile can be signed in.
#
#   docker compose run --rm --service-ports signin
#
# It lives under docker/ because that is where the image picks it up, but it is
# plain shell and the native install runs the same script directly:
#
#   REWARDS_DATA_DIR=/var/lib/rewards-farmer/data-dir docker/signin.sh
#
# then point a VNC client at 127.0.0.1:5900. The compose file publishes that
# port on the host's loopback only, so from another machine tunnel it:
#
#   ssh -N -L 5900:127.0.0.1:5900 you@your-proxmox-container
#
# Sign in on rewards.bing.com and on bing.com, accept the consent banner if the
# market shows one, then close the browser window. The sign-in is written to the
# profile in data-dir, which is the same directory the daily run uses.
#
# Why this exists at all: the sign-in lives in the browser profile, and a
# profile signed in on Windows or macOS cannot be read here, because those wrap
# the cookie encryption key with something the container has no access to. On
# Linux, with no keyring running, Chromium falls back to a fixed key, so a
# profile signed in in this container works in this container.
set -eu

DISPLAY_NUMBER="${REWARDS_SIGNIN_DISPLAY:-99}"
export DISPLAY=":${DISPLAY_NUMBER}"

PROFILE_DIR="${REWARDS_DATA_DIR:-/app/data-dir}"

# One account's directory, matching what accounts.py does with REWARDS_ACCOUNTS.
# Signing in to the wrong directory looks like it worked and earns nothing.
if [ -n "${REWARDS_SIGNIN_ACCOUNT:-}" ]; then
	PROFILE_DIR="${PROFILE_DIR}/${REWARDS_SIGNIN_ACCOUNT}"
fi

mkdir -p "$PROFILE_DIR"

cleanup() {
	# Edge writes the profile as it exits, and a profile whose browser was
	# killed keeps a lock naming a process that no longer exists.
	[ -n "${EDGE_PID:-}" ] && kill "$EDGE_PID" 2>/dev/null || true
	[ -n "${VNC_PID:-}" ] && kill "$VNC_PID" 2>/dev/null || true
	[ -n "${WM_PID:-}" ] && kill "$WM_PID" 2>/dev/null || true
	[ -n "${XVFB_PID:-}" ] && kill "$XVFB_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

Xvfb "$DISPLAY" -screen 0 1600x1000x24 -nolisten tcp &
XVFB_PID=$!

# Give the server a moment to create its socket. Edge started against a display
# that is not up yet exits immediately.
for _ in 1 2 3 4 5 6 7 8 9 10; do
	[ -e "/tmp/.X11-unix/X${DISPLAY_NUMBER}" ] && break

	sleep 0.5
done

fluxbox >/dev/null 2>&1 &
WM_PID=$!

# 0.0.0.0 inside the container, because a published port is forwarded to the
# container's interface and never reaches its loopback. What keeps this off the
# network is the host side: compose publishes it on 127.0.0.1 only.
if [ -n "${REWARDS_VNC_PASSWORD:-}" ]; then
	x11vnc -display "$DISPLAY" -forever -shared -passwd "$REWARDS_VNC_PASSWORD" -quiet &
else
	echo "signin: no REWARDS_VNC_PASSWORD set; the port is published on the host's"
	echo "        loopback only, so connect from the host or through an SSH tunnel."
	x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet &
fi

VNC_PID=$!

echo "signin: VNC ready on port 5900. Profile: ${PROFILE_DIR}"
echo "signin: sign in on rewards.bing.com and bing.com, then close the browser."

microsoft-edge \
	--no-sandbox \
	--disable-dev-shm-usage \
	--no-first-run \
	--no-default-browser-check \
	--user-data-dir="$PROFILE_DIR" \
	--profile-directory="${REWARDS_PROFILE_NAME:-Default}" \
	--window-size=1600,1000 \
	https://rewards.bing.com/ &
EDGE_PID=$!

# Ends when the browser window is closed, which is the signal that the human is
# done. Everything else is torn down by the trap. A browser that exits non-zero
# still means the same thing here, so its code is not this script's.
wait "$EDGE_PID" || true

echo "signin: browser closed. The profile in ${PROFILE_DIR} is what the daily run uses."
