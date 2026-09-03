"""A daily run, for a machine nobody is sitting at.

Rewards resets once a day, so the useful unit of work is one run per day, and
the useful place to run it is a machine that is already on - a Proxmox home
server, in the case this was written for. What that needs from a scheduler is
not a cron line:

  * The points are gone if the run is skipped, so a machine that was off at the
    scheduled time runs as soon as it comes back rather than waiting a day.
  * Once a day, and only once. A container restart, a `docker compose up` after
    an edit, a host reboot - none of them may start a second run for a day that
    is already earned.
  * Never at 03:00:00 exactly. A run that starts at the same second every day
    is a pattern, and the whole rest of this project exists to not look like a
    script. The time is offset by a random amount that is fixed per day, so a
    restart does not reroll it and hand the same day two different times.
  * A wedged run has to end. Selenium waits are bounded, but the browser itself
    can stop answering, and an unattended install that hangs on a Tuesday earns
    nothing until someone notices. The run is a child process with a deadline
    and the whole process group is killed when it passes.
  * A failure gets another go. Bing serving a broken page for ten minutes
    should not cost the day.

	python src/scheduler.py             # the daily loop, until stopped
	python src/scheduler.py --once      # one run now, exit with its code
	python src/scheduler.py --next      # when the next run would be
	python src/scheduler.py --status    # what the last run did
	python src/scheduler.py --health    # quiet, exit code only

Configuration is environment variables, so it is the same under Docker, systemd
and a shell:

	REWARDS_SCHEDULE              HH:MM local time, default 09:00
	REWARDS_JITTER_MINUTES        random offset after that time, default 90
	REWARDS_TZ                    IANA zone, default the system's
	REWARDS_CATCH_UP              1 to run a missed day at startup, default 1
	REWARDS_RETRY_MINUTES         wait before retrying a failed run, default 30
	REWARDS_MAX_RETRIES           retries per day, default 2
	REWARDS_RUN_TIMEOUT_MINUTES   kill a run that outlasts this, default 120
"""

import argparse
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone, tzinfo
from pathlib import Path

import log_utils
import paths
import run_state

logger = logging.getLogger(__name__)

SCHEDULE_ENV = "REWARDS_SCHEDULE"
JITTER_ENV = "REWARDS_JITTER_MINUTES"
TZ_ENV = "REWARDS_TZ"
CATCH_UP_ENV = "REWARDS_CATCH_UP"
RETRY_MINUTES_ENV = "REWARDS_RETRY_MINUTES"
MAX_RETRIES_ENV = "REWARDS_MAX_RETRIES"
TIMEOUT_ENV = "REWARDS_RUN_TIMEOUT_MINUTES"

DEFAULT_SCHEDULE = "09:00"
DEFAULT_JITTER_MINUTES = 90
DEFAULT_RETRY_MINUTES = 30
DEFAULT_MAX_RETRIES = 2
DEFAULT_TIMEOUT_MINUTES = 120

# Exit code for a run the scheduler killed, matching what coreutils' timeout
# reports, so a log line reading 124 means the same thing here as anywhere.
TIMEOUT_EXIT_CODE = 124

# The loop wakes at least this often even when the target is hours away. Long
# sleeps and home servers do not mix: a suspended VM, a host that was paused
# for a backup and an NTP step after boot all move the clock underneath a
# sleeping process, and a run that was due during the jump has to be noticed
# rather than slept through.
MAX_SLEEP_SECONDS = 60

# How long a killed run gets to shut its browser down before it is killed
# outright. Edge writes the profile on exit, and a profile torn out from under
# it comes back with a lock file that stops the next run from starting.
GRACE_SECONDS = 20


def _int_env(variable: str, default: int, minimum: int = 0) -> int:
	"""An integer from the environment, falling back rather than raising.

	A typo in a compose file must not take down an install that would otherwise
	run unattended for months.
	"""
	raw = os.environ.get(variable, "").strip()

	if not raw:
		return default

	try:
		value = int(raw)
	except ValueError:
		logger.warning("%s=%r is not a number, using %s", variable, raw, default)

		return default

	if value < minimum:
		logger.warning("%s=%s is below %s, using %s", variable, value, minimum, minimum)

		return minimum

	return value


def _bool_env(variable: str, default: bool) -> bool:
	raw = os.environ.get(variable, "").strip().lower()

	if not raw:
		return default

	return raw in ("1", "true", "yes", "on")


def parse_time(text: str) -> tuple[int, int]:
	"""`HH:MM` as a pair. Raises ValueError on anything else."""
	parts = text.strip().split(":")

	if len(parts) != 2:
		raise ValueError(f"expected HH:MM, got {text!r}")

	hour, minute = (int(part) for part in parts)

	if not (0 <= hour <= 23 and 0 <= minute <= 59):
		raise ValueError(f"{text!r} is not a time of day")

	return hour, minute


def _zone(name: str) -> tzinfo | None:
	"""The named zone, or None when it cannot be resolved."""
	if not name:
		return None

	try:
		from zoneinfo import ZoneInfo

		return ZoneInfo(name)
	except Exception:
		# tzdata missing from the image, or a typo in the zone name. Both end up
		# on the system zone, which is a working schedule at the wrong hour
		# rather than no schedule at all.
		logger.warning("Unknown timezone %r, using the system timezone", name)

		return None


def local_zone() -> tzinfo:
	"""The zone the schedule is expressed in."""
	return _zone(os.environ.get(TZ_ENV, "").strip()) or datetime.now().astimezone().tzinfo


@dataclass(frozen=True)
class Schedule:
	"""When to run, and how hard to try."""

	hour: int = 9
	minute: int = 0
	jitter_minutes: int = DEFAULT_JITTER_MINUTES
	catch_up: bool = True
	retry_minutes: int = DEFAULT_RETRY_MINUTES
	max_retries: int = DEFAULT_MAX_RETRIES
	timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES
	zone: tzinfo | None = None

	@classmethod
	def from_env(cls) -> "Schedule":
		raw = os.environ.get(SCHEDULE_ENV, DEFAULT_SCHEDULE).strip() or DEFAULT_SCHEDULE

		try:
			hour, minute = parse_time(raw)
		except ValueError as exc:
			logger.warning("%s: %s, using %s", SCHEDULE_ENV, exc, DEFAULT_SCHEDULE)

			hour, minute = parse_time(DEFAULT_SCHEDULE)

		return cls(
			hour=hour,
			minute=minute,
			jitter_minutes=_int_env(JITTER_ENV, DEFAULT_JITTER_MINUTES),
			catch_up=_bool_env(CATCH_UP_ENV, True),
			retry_minutes=_int_env(RETRY_MINUTES_ENV, DEFAULT_RETRY_MINUTES, minimum=1),
			max_retries=_int_env(MAX_RETRIES_ENV, DEFAULT_MAX_RETRIES),
			timeout_minutes=_int_env(TIMEOUT_ENV, DEFAULT_TIMEOUT_MINUTES, minimum=1),
			zone=local_zone(),
		)

	@property
	def tz(self) -> tzinfo:
		return self.zone or datetime.now().astimezone().tzinfo

	def now(self) -> datetime:
		return datetime.now(self.tz)

	def describe(self) -> str:
		window = f"{self.hour:02d}:{self.minute:02d}"

		if self.jitter_minutes:
			window += f" (+ up to {self.jitter_minutes} min)"

		return f"{window} {self.tz}"


def jitter_for(day: date, seed: str, minutes: int) -> timedelta:
	"""The offset for one day. Same day and same install, same answer.

	Derived rather than drawn, so that restarting the scheduler at noon does not
	move a run that was already scheduled for 09:47 and let the day run twice.
	The seed is per install, so two machines pointed at the same schedule do not
	start in lockstep.
	"""
	if minutes <= 0:
		return timedelta(0)

	generator = random.Random(f"{seed}:{day.isoformat()}")

	return timedelta(seconds=generator.randrange(0, minutes * 60 + 1))


def slot_for(day: date, schedule: Schedule, seed: str) -> datetime:
	"""The moment the run is due on `day`.

	On the two days a year a zone shifts, the configured time can land in an
	hour that does not exist or happens twice. Python resolves both to a real
	instant, an hour early or late, which is a run at a slightly odd time once a
	year rather than a run that never happens.
	"""
	base = datetime.combine(day, clock_time(schedule.hour, schedule.minute), tzinfo=schedule.tz)

	return base + jitter_for(day, seed, schedule.jitter_minutes)


def next_run(
	now: datetime,
	last_success: date | None,
	schedule: Schedule,
	seed: str,
	attempts: int = 0,
) -> datetime:
	"""When the next run is due.

	Today's slot if it has not passed, tomorrow's if today is already earned or
	has had its attempts, and immediately if today's slot passed without a
	successful run - which is the case that matters on a machine that is not on
	all the time.

	`attempts` is how many runs today has already had. Without it, catching up
	is unbounded: a day that fails every attempt is still a day whose slot has
	passed with no success, so the answer would be "run now" for the rest of it,
	and an install that is broken - an expired sign-in, say - would start a
	browser continuously until someone noticed.
	"""
	today = now.date()

	if last_success == today or attempts > schedule.max_retries:
		return slot_for(today + timedelta(days=1), schedule, seed)

	today_slot = slot_for(today, schedule, seed)

	if now < today_slot:
		return today_slot

	if schedule.catch_up:
		return now

	return slot_for(today + timedelta(days=1), schedule, seed)


def last_success_date(state: dict, zone: tzinfo) -> date | None:
	"""The day the last successful run finished, in the schedule's own zone.

	The state file stores the instant, not the day. A run that finishes at
	00:30 UTC finished on a different date in Berlin, and "has today been
	earned" has to be answered on the calendar the schedule is written in.
	"""
	raw = state.get("last_success")

	if not raw:
		return None

	try:
		when = datetime.fromisoformat(str(raw))
	except ValueError:
		return None

	if when.tzinfo is None:
		when = when.replace(tzinfo=timezone.utc)

	return when.astimezone(zone).date()


def attempts_today(state: dict, day: date) -> int:
	"""How many runs `day` has already had.

	Kept in the state file rather than in a variable, so the budget survives a
	restart. A container that crash-loops must not get a fresh set of attempts
	each time it comes back.
	"""
	record = state.get("attempts")

	if not isinstance(record, dict) or record.get("date") != day.isoformat():
		return 0

	try:
		return int(record.get("count", 0))
	except (TypeError, ValueError):
		return 0


def record_attempt(day: date) -> None:
	"""Count a run against `day`'s budget, before it starts."""
	def mutate(state: dict) -> None:
		state["attempts"] = {
			"date": day.isoformat(),
			"count": attempts_today(state, day) + 1,
		}

	run_state.update(mutate)


def install_seed() -> str:
	"""A per install string for the jitter, created once and then reused."""
	state = run_state.read()
	seed = state.get("jitter_seed")

	if isinstance(seed, str) and seed:
		return seed

	seed = f"{random.getrandbits(64):016x}"

	run_state.update(lambda stored: stored.setdefault("jitter_seed", seed))

	# setdefault above loses a race with another process by design: whichever
	# seed is in the file afterwards is the one every process must use, so read
	# it back rather than trusting the one just generated.
	return run_state.read().get("jitter_seed", seed)


class Runner:
	"""Starts `main.py` as a child process and waits for it.

	A child rather than a function call. Three reasons, all of them about a
	process that has to stay up for months: a run that leaks a driver or a
	window leaks it into something that then exits, an unhandled crash takes the
	run down instead of the scheduler, and a browser that stops answering can be
	killed, which a call inside this process cannot be.
	"""

	def __init__(self, timeout_minutes: int):
		self.timeout_minutes = timeout_minutes
		# Written here and read from the signal handler, which runs on this same
		# thread. A lock around it would be the wrong tool twice over: it guards
		# nothing, and a signal arriving while it was held would deadlock the
		# handler against the thread it just interrupted.
		self._process: subprocess.Popen | None = None

	def run(self) -> int:
		script = Path(__file__).resolve().parent / "main.py"
		# -u so the child's output reaches `docker logs` and the journal as it
		# happens rather than when its buffer fills.
		command = [sys.executable, "-u", str(script)]

		environment = dict(os.environ)
		# The child is not attached to a terminal, and main() must not wait for
		# a keypress that is never coming. It checks stdin as well, but a
		# scheduled run says so explicitly rather than inferring it.
		environment["REWARDS_SCHEDULED"] = "1"

		# Monotonic, so a clock step during the run - an NTP correction after a
		# boot is the usual one - does not turn a twenty minute run into a
		# negative one in the log and the state file.
		started = time.monotonic()

		logger.info("Starting run: %s", " ".join(command))

		try:
			process = subprocess.Popen(
				command,
				cwd=str(paths.REPO_ROOT),
				env=environment,
				# Its own process group, so a timeout kills the browser and the
				# driver the run started, not just the python that started them.
				start_new_session=os.name != "nt",
			)
		except OSError as exc:
			logger.error("[FAIL] could not start the run: %s", exc)

			return 1

		self._process = process

		try:
			code = process.wait(timeout=self.timeout_minutes * 60)
		except subprocess.TimeoutExpired:
			logger.error(
				"[FAIL] the run passed its %s minute deadline, killing it",
				self.timeout_minutes
			)

			self._kill(process)

			code = TIMEOUT_EXIT_CODE
		finally:
			self._process = None

		elapsed = timedelta(seconds=time.monotonic() - started)

		# A run that was killed never reached the end of its own bookkeeping, so
		# the state file still says it is in progress. Close it out from here,
		# where the exit code is known.
		if run_state.reconcile_interrupted(code, elapsed.total_seconds()):
			logger.debug("Recorded the outcome of a run that could not record it itself.")

		if code == 0:
			logger.info("Run finished in %s", _duration(elapsed))
		else:
			logger.warning("Run finished in %s with exit code %s", _duration(elapsed), code)

		return code

	def stop(self) -> None:
		"""End a run in progress, if there is one. Safe to call from a signal."""
		process = self._process

		if process is not None:
			self._kill(process)

	def _kill(self, process: subprocess.Popen) -> None:
		"""Signal the run's whole process group, then insist."""
		# SIGKILL does not exist on Windows, where the branch below never signals
		# a group anyway.
		hard = getattr(signal, "SIGKILL", signal.SIGTERM)

		for signal_number, wait in ((signal.SIGTERM, GRACE_SECONDS), (hard, 5)):
			if process.poll() is not None:
				return

			try:
				if os.name == "nt":
					process.kill()
				else:
					os.killpg(os.getpgid(process.pid), signal_number)
			except (OSError, AttributeError):
				# Already gone, or a platform without process groups.
				process.kill()

			try:
				process.wait(timeout=wait)

				return
			except subprocess.TimeoutExpired:
				continue


def _duration(delta: timedelta) -> str:
	seconds = int(delta.total_seconds())

	return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m{seconds % 60:02d}s"


def _sleep_until(target: datetime, schedule: Schedule, stop: threading.Event) -> bool:
	"""Wait for `target`. Returns False when asked to stop instead.

	Woken regularly rather than sleeping the whole interval, so that the clock
	moving - a resumed VM, an NTP step, a zone change - is noticed within a
	minute instead of at the end of a sleep computed against the old time.
	"""
	while not stop.is_set():
		remaining = (target - schedule.now()).total_seconds()

		if remaining <= 0:
			return True

		if stop.wait(min(remaining, MAX_SLEEP_SECONDS)):
			return False

	return False


def loop(schedule: Schedule, stop: threading.Event, runner: Runner) -> int:
	"""Run once a day until stopped. Returns an exit code for the process."""
	seed = install_seed()

	logger.info("Daily run scheduled for %s", schedule.describe())
	logger.info("Profiles: %s", paths.data_dir())
	logger.info("State:    %s", run_state.state_file())

	# A container that was killed mid-run leaves the state saying a run is in
	# progress. Taking the lock is what proves otherwise: if it is free, nothing
	# is running, whatever the file says. If it is held, a run really is in
	# flight and this is left alone.
	with run_state.run_lock() as free:
		if free and run_state.reconcile_interrupted():
			logger.warning("A previous run was interrupted; recording it as failed.")

	while not stop.is_set():
		state = run_state.read()
		now = schedule.now()
		attempts = attempts_today(state, now.date())
		target = next_run(now, last_success_date(state, schedule.tz), schedule, seed, attempts)

		run_state.update(lambda stored: stored.update({"next_run": target.isoformat(timespec="seconds")}))

		if target > now:
			logger.info("Next run at %s", target.isoformat(timespec="seconds"))

		if not _sleep_until(target, schedule, stop):
			break

		day = schedule.now().date()

		record_attempt(day)

		code = runner.run()

		if code == 0 or stop.is_set():
			continue

		spent = attempts_today(run_state.read(), day)

		if spent > schedule.max_retries:
			logger.error("[FAIL] no attempt left for today, waiting for tomorrow's run.")

			continue

		retry_at = schedule.now() + timedelta(minutes=schedule.retry_minutes)

		# A retry that would land after midnight is not a retry, it is tomorrow's
		# run happening early, and the loop schedules that one on its own.
		if retry_at.date() != day:
			logger.warning("Not retrying: the day is over.")

			continue

		logger.warning(
			"Retry %s/%s at %s",
			spent, schedule.max_retries, retry_at.isoformat(timespec="seconds")
		)

		_sleep_until(retry_at, schedule, stop)

	logger.info("Scheduler stopped.")

	return 0


def _install_signal_handlers(stop: threading.Event, runner: Runner) -> None:
	def handle(signal_number, _frame):
		logger.info("Received %s, stopping.", signal.Signals(signal_number).name)
		stop.set()
		# The run is a child in its own process group, so it does not get the
		# terminal's Ctrl-C or the container's SIGTERM. Pass it on, otherwise
		# `docker stop` waits out its timeout and then kills a browser mid-write.
		runner.stop()

	for name in ("SIGTERM", "SIGINT"):
		number = getattr(signal, name, None)

		if number is not None:
			try:
				signal.signal(number, handle)
			except ValueError:
				# Not the main thread. Only reachable from a test.
				pass


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(
		prog="scheduler",
		description="Run the rewards bot once a day, unattended.",
	)
	group = parser.add_mutually_exclusive_group()
	group.add_argument("--once", action="store_true", help="run now, once, and exit with its code")
	group.add_argument("--next", action="store_true", help="print when the next run is due and exit")
	group.add_argument("--status", action="store_true", help="print what the last run did and exit")
	group.add_argument(
		"--health",
		action="store_true",
		help="exit non-zero when no run has succeeded recently; prints nothing",
	)

	arguments = parser.parse_args(argv)

	log_utils.setup_logging()

	if arguments.health:
		return 1 if run_state.is_stale() else 0

	if arguments.status:
		schedule = Schedule.from_env()

		print(f"schedule:        {schedule.describe()}")
		print(run_state.summary())

		return 0

	schedule = Schedule.from_env()

	if arguments.next:
		state = run_state.read()
		now = schedule.now()
		target = next_run(
			now,
			last_success_date(state, schedule.tz),
			schedule,
			install_seed(),
			attempts_today(state, now.date()),
		)

		print(target.isoformat(timespec="seconds"))

		return 0

	runner = Runner(schedule.timeout_minutes)
	stop = threading.Event()

	_install_signal_handlers(stop, runner)

	if arguments.once:
		return runner.run()

	return loop(schedule, stop, runner)


if __name__ == "__main__":
	sys.exit(main())
