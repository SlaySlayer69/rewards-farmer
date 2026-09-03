#!/bin/sh
# Turns the container's argument into one of the ways this thing is run, so a
# home server install is `docker compose up -d` and everything else is a verb
# rather than a python invocation to remember.
#
#   daily    the scheduler, one run a day, until the container stops (default)
#   once     one run now, then exit with its result
#   status   what the last run did
#   next     when the next run is due
#   health   silent, exit code only, used by the healthcheck
#   signin   a browser over VNC, to sign the profile in once
#   shell    a shell in the container
#
# Anything else is executed as given, so `docker compose run --rm bot python -V`
# still works.
set -eu

# The compose file mounts these, and a bind mount from the host arrives owned by
# whoever owns it there. Fail early and clearly rather than halfway through the
# first run with a traceback about a directory.
for directory in "${REWARDS_DATA_DIR:-/app/data-dir}" "${REWARDS_STATE_DIR:-/app/state}"; do
	if ! mkdir -p "$directory" 2>/dev/null || [ ! -w "$directory" ]; then
		echo "entrypoint: $directory is not writable by uid $(id -u)." >&2
		echo "            On the host: sudo chown -R $(id -u):$(id -g) <that directory>" >&2

		exit 1
	fi
done

command="${1:-daily}"

if [ "$#" -gt 0 ]; then
	shift
fi

case "$command" in
	daily)  exec python -u src/scheduler.py "$@" ;;
	once)   exec python -u src/scheduler.py --once "$@" ;;
	status) exec python -u src/scheduler.py --status "$@" ;;
	next)   exec python -u src/scheduler.py --next "$@" ;;
	health) exec python -u src/scheduler.py --health "$@" ;;
	signin) exec /usr/local/bin/signin.sh "$@" ;;
	shell)  exec /bin/sh "$@" ;;
	*)      exec "$command" "$@" ;;
esac
