"""What the last run did, written where something else can read it.

Nobody watches an unattended run. The only way to answer "is this thing still
earning?" without reading a day of log output is a small file the run keeps up
to date, so that is what this is: one JSON document holding the outcome of the
last run, a short history, and the counters the scheduler needs to decide
whether today is already done.

	python src/scheduler.py --status

reads it, and the container healthcheck uses the same code path, so a stalled
install shows up as unhealthy instead of quietly earning nothing.

The lock lives here too. Chromium allows one process per profile directory, so
a scheduled run starting while a manual one is open fails halfway through with
an error about the browser crashing. Refusing to start is both clearer and
cheaper.
"""

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import paths

try:
	import fcntl
except ImportError:
	# Windows. Every caller treats the lock as advisory and carries on without
	# it, which is the pre-existing behaviour there.
	fcntl = None

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"
LOCK_FILE = "run.lock"

# Enough history to see a pattern - a task that fails every day, a run that
# started failing after a UI change - without the file growing forever.
HISTORY_LIMIT = 14

# How long a successful run may be absent before the install is considered
# broken rather than merely idle. Slightly over two days, so a single missed
# day and its retries do not raise an alarm.
STALE_AFTER_HOURS = 50


def state_file() -> Path:
	return paths.state_dir() / STATE_FILE


def lock_file() -> Path:
	return paths.state_dir() / LOCK_FILE


def _now() -> str:
	# UTC with an offset, so a state file written before a DST change still
	# sorts and compares against one written after it.
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read() -> dict:
	"""The stored state, or an empty dict.

	Never raises. A state file that was truncated by a power cut must not stop
	the next run from happening.
	"""
	try:
		with open(state_file(), encoding="utf-8") as handle:
			stored = json.load(handle)
	except (OSError, ValueError):
		return {}

	return stored if isinstance(stored, dict) else {}


def update(mutate: Callable[[dict], None]) -> dict:
	"""Read, mutate, write atomically. Returns the state that was written.

	The scheduler and the run it spawns both write here, so the read and the
	write are held under the same lock the run itself takes, and the file is
	replaced rather than rewritten in place. A reader therefore sees either the
	old document or the new one, never half of each.
	"""
	directory = paths.ensure_dir(paths.state_dir())
	target = state_file()

	with _exclusive(directory / (STATE_FILE + ".lock")):
		state = read()

		try:
			mutate(state)
		except Exception:
			logger.debug("state mutation failed", exc_info=True)

			return state

		temporary = target.with_suffix(".tmp")

		try:
			with open(temporary, "w", encoding="utf-8") as handle:
				json.dump(state, handle, indent=2, sort_keys=True)
				handle.write("\n")
				handle.flush()
				os.fsync(handle.fileno())

			os.replace(temporary, target)
		except OSError as exc:
			# A read-only volume, a full disk. Bookkeeping is not worth failing
			# a run over.
			logger.warning("Could not write %s: %s", target, exc)

	return state


@contextmanager
def _exclusive(path: Path):
	"""Hold an exclusive lock on `path` for the duration of the block.

	Blocking, because the sections it guards are a few milliseconds of JSON.
	Degrades to no locking where flock is unavailable or the file cannot be
	created, since serialising state writes is a nicety and refusing to record
	anything would not be.
	"""
	if fcntl is None:
		yield

		return

	try:
		handle = open(path, "a+")
	except OSError:
		yield

		return

	try:
		fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

		yield
	finally:
		try:
			fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
		finally:
			handle.close()


@contextmanager
def run_lock():
	"""Yields True when this process may run, False when another one already is.

	The caller decides what to do with a False: `main` reports it and stops
	rather than starting a browser against a profile that is already open.
	"""
	if fcntl is None:
		yield True

		return

	paths.ensure_dir(paths.state_dir())

	try:
		handle = open(lock_file(), "a+")
	except OSError as exc:
		logger.debug("Could not open the run lock (%s), continuing without it", exc)

		yield True

		return

	try:
		try:
			fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
		except OSError:
			yield False

			return

		# Whoever holds the lock names itself, so a stuck run can be found and
		# killed without guessing. The lock itself is the flock, not this text:
		# it is released by the kernel when the process dies, however it dies,
		# so there is no stale lock to clean up after a power cut.
		try:
			handle.seek(0)
			handle.truncate()
			handle.write(f"pid={os.getpid()} since={_now()}\n")
			handle.flush()
		except OSError:
			pass

		yield True
	finally:
		try:
			fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
		finally:
			handle.close()


def record_run_start() -> None:
	def mutate(state: dict) -> None:
		state["last_started"] = _now()
		state["running"] = True

	update(mutate)


def record_run_finish(exit_code: int, seconds: float, accounts_ran: int = 0) -> None:
	"""Store the outcome of a finished run and roll the history forward."""
	finished = _now()

	def mutate(state: dict) -> None:
		state["running"] = False
		state["last_finished"] = finished
		state["last_exit_code"] = exit_code
		state["last_duration_seconds"] = round(seconds, 1)
		state["last_accounts_ran"] = accounts_ran

		if exit_code == 0:
			# Stored as an instant rather than a date. Whether today's run has
			# happened is a question about the wall clock the schedule is
			# expressed in, and only the scheduler knows which zone that is, so
			# it converts this itself instead of two modules having to agree.
			state["last_success"] = finished
			state["consecutive_failures"] = 0
		else:
			state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1

		history = state.get("history")

		if not isinstance(history, list):
			history = []

		history.append({
			"finished": finished,
			"exit_code": exit_code,
			"duration_seconds": round(seconds, 1),
			"accounts_ran": accounts_ran,
		})

		state["history"] = history[-HISTORY_LIMIT:]

	update(mutate)


@contextmanager
def recorded_run():
	"""Record a run around the block, whatever it exits with.

	Used by `main` so that a direct invocation - cron, a systemd timer, a human
	- lands in the same state file as a scheduled one, and `--status` means the
	same thing under every install.
	"""
	started = time.monotonic()
	record_run_start()

	outcome = {"exit_code": 1, "accounts_ran": 0}

	try:
		yield outcome
	except BaseException:
		record_run_finish(1, time.monotonic() - started, 0)

		raise

	record_run_finish(
		int(outcome.get("exit_code", 1)),
		time.monotonic() - started,
		int(outcome.get("accounts_ran", 0)),
	)


def reconcile_interrupted(exit_code: int = 1, seconds: float = 0.0) -> bool:
	"""Close out a run that was killed before it could record its own outcome.

	`recorded_run` writes the outcome on the way out, and a run that is killed
	outright never gets there: the timeout the scheduler enforces, an OOM kill,
	the host losing power. The state file is then stuck reading "a run is in
	progress" forever, which makes `--status` lie and hides every later failure
	behind a run that ended days ago.

	Returns whether there was anything to close out.
	"""
	if not read().get("running"):
		return False

	record_run_finish(exit_code, seconds)

	return True


def is_stale(state: dict | None = None, hours: int = STALE_AFTER_HOURS) -> bool:
	"""Whether a successful run is overdue by enough to call the install broken.

	A state file that has never recorded a success is not stale on its own: a
	fresh install has not had its first scheduled run yet, and reporting that
	as a failure would make every new container unhealthy for a day.
	"""
	if state is None:
		state = read()

	last_success = state.get("last_success")

	if not last_success:
		return False

	try:
		when = datetime.fromisoformat(last_success)
	except ValueError:
		return False

	if when.tzinfo is None:
		when = when.replace(tzinfo=timezone.utc)

	return (datetime.now(timezone.utc) - when).total_seconds() > hours * 3600


def summary(state: dict | None = None) -> str:
	"""The state file as a few lines meant for a human."""
	if state is None:
		state = read()

	if not state:
		return f"No run recorded yet ({state_file()})."

	lines = [
		f"state file:      {state_file()}",
		f"last started:    {state.get('last_started', '-')}",
		f"last finished:   {state.get('last_finished', '-')}",
		f"last exit code:  {state.get('last_exit_code', '-')}",
		f"last success:    {state.get('last_success', 'never')}",
		f"failures in row: {state.get('consecutive_failures', 0)}",
		f"next run:        {state.get('next_run', '-')}",
	]

	if state.get("running"):
		lines.append("a run is in progress")

	if is_stale(state):
		lines.append(f"STALE: no successful run in the last {STALE_AFTER_HOURS} hours")

	return "\n".join(lines)
