"""Tests for the daily schedule and for the paths a scheduled run depends on.

The scheduler decides one thing - when the next run is - and gets three cases
wrong at nobody's cost but the user's: running twice on a day that is already
earned, skipping a day the machine was off for, and moving a run that was
already scheduled because something restarted. Those are what most of this
covers.

The path tests are here for the same reason. Every path used to be resolved
against the working directory, which is fine when a human starts the bot from
the repository root and wrong for every scheduled run, and the failure is
silent: an empty profile earns nothing and reports success.

None of these start a browser or sleep.

	python -m unittest discover -s tests
"""

import logging
import os
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, time as clock_time, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import paths
import run_state
import scheduler

BERLIN = "Europe/Berlin"

# Every environment variable these tests write, so one case cannot leak into
# the next through the process it shares with all of them.
TOUCHED = (
	scheduler.SCHEDULE_ENV,
	scheduler.JITTER_ENV,
	scheduler.TZ_ENV,
	scheduler.CATCH_UP_ENV,
	scheduler.RETRY_MINUTES_ENV,
	scheduler.MAX_RETRIES_ENV,
	scheduler.TIMEOUT_ENV,
	paths.DATA_DIR_ENV,
	paths.STATE_DIR_ENV,
	paths.NOUNS_ENV,
	paths.VISUAL_SEARCH_ENV,
)


def zone(name: str = BERLIN):
	from zoneinfo import ZoneInfo

	return ZoneInfo(name)


class EnvironmentTestCase(unittest.TestCase):
	"""Restores everything these tests reach into, so ordering cannot matter."""

	def setUp(self):
		for variable in TOUCHED:
			self.addCleanup(_restore, variable, os.environ.get(variable))


def _restore(variable, value):
	if value is None:
		os.environ.pop(variable, None)
	else:
		os.environ[variable] = value


class TestScheduleConfiguration(EnvironmentTestCase):
	"""Every one of these logs a warning by design, which is the point of them.

	The assertions report the outcome here, so the warnings themselves are
	silenced to keep the suite's output to what unittest prints.
	"""

	def setUp(self):
		super().setUp()

		logging.disable(logging.CRITICAL)
		self.addCleanup(logging.disable, logging.NOTSET)

	def test_a_time_of_day_is_read_from_the_environment(self):
		os.environ[scheduler.SCHEDULE_ENV] = "06:45"

		schedule = scheduler.Schedule.from_env()

		self.assertEqual((schedule.hour, schedule.minute), (6, 45))

	def test_an_unusable_time_falls_back_instead_of_raising(self):
		# A typo in a compose file must not take down an install that would
		# otherwise run unattended for months.
		for value in ("nine", "25:00", "09:99", "09", "", "9-30", "09:00:00"):
			with self.subTest(value=value):
				os.environ[scheduler.SCHEDULE_ENV] = value

				schedule = scheduler.Schedule.from_env()

				self.assertEqual((schedule.hour, schedule.minute), (9, 0))

	def test_unusable_numbers_fall_back_too(self):
		os.environ[scheduler.JITTER_ENV] = "lots"
		os.environ[scheduler.MAX_RETRIES_ENV] = ""
		os.environ[scheduler.RETRY_MINUTES_ENV] = "-5"

		schedule = scheduler.Schedule.from_env()

		self.assertEqual(schedule.jitter_minutes, scheduler.DEFAULT_JITTER_MINUTES)
		self.assertEqual(schedule.max_retries, scheduler.DEFAULT_MAX_RETRIES)
		# Clamped rather than defaulted: a retry interval of zero would spin.
		self.assertEqual(schedule.retry_minutes, 1)

	def test_an_unknown_timezone_does_not_raise(self):
		os.environ[scheduler.TZ_ENV] = "Middle/Earth"

		self.assertIsNotNone(scheduler.Schedule.from_env().tz)

	def test_the_named_timezone_is_used(self):
		os.environ[scheduler.TZ_ENV] = BERLIN

		schedule = scheduler.Schedule.from_env()
		slot = scheduler.slot_for(date(2026, 1, 15), schedule, seed="seed")

		self.assertEqual(slot.utcoffset(), timedelta(hours=1))


class TestJitter(unittest.TestCase):
	def test_the_same_day_always_gets_the_same_offset(self):
		# The one that matters: a scheduler restarted at noon must not move a
		# run it already scheduled for 09:47, because a moved run is a second
		# run of a day that is already earned.
		day = date(2026, 3, 4)
		first = scheduler.jitter_for(day, "seed", 90)

		for _ in range(50):
			self.assertEqual(scheduler.jitter_for(day, "seed", 90), first)

	def test_it_stays_inside_the_configured_window(self):
		for offset in range(400):
			day = date(2026, 1, 1) + timedelta(days=offset)
			jitter = scheduler.jitter_for(day, "seed", 90)

			self.assertGreaterEqual(jitter, timedelta(0))
			self.assertLessEqual(jitter, timedelta(minutes=90))

	def test_different_days_do_not_all_land_on_the_same_minute(self):
		offsets = {
			scheduler.jitter_for(date(2026, 1, 1) + timedelta(days=n), "seed", 90)
			for n in range(60)
		}

		self.assertGreater(len(offsets), 30)

	def test_two_installs_do_not_start_in_lockstep(self):
		day = date(2026, 3, 4)

		self.assertNotEqual(
			scheduler.jitter_for(day, "one", 90),
			scheduler.jitter_for(day, "two", 90),
		)

	def test_zero_means_exactly_the_configured_time(self):
		self.assertEqual(scheduler.jitter_for(date(2026, 3, 4), "seed", 0), timedelta(0))


class NextRunTestCase(unittest.TestCase):
	"""next_run() against a fixed clock, with the jitter turned off.

	Jitter is covered above; leaving it on here would only make every expected
	time a range.
	"""

	SEED = "test"

	def schedule(self, **overrides):
		defaults = dict(hour=9, minute=0, jitter_minutes=0, zone=zone())
		defaults.update(overrides)

		return scheduler.Schedule(**defaults)

	def at(self, year, month, day, hour, minute=0):
		return datetime(year, month, day, hour, minute, tzinfo=zone())

	def next_run(self, now, last_success, attempts=0, **overrides):
		return scheduler.next_run(
			now, last_success, self.schedule(**overrides), self.SEED, attempts
		)


class TestNextRun(NextRunTestCase):
	def test_before_todays_slot_it_waits_for_it(self):
		now = self.at(2026, 5, 4, 7)

		self.assertEqual(self.next_run(now, None), self.at(2026, 5, 4, 9))

	def test_a_day_already_earned_is_not_run_again(self):
		# A container restart, a `docker compose up` after an edit, a host
		# reboot. None of them may earn the same day twice.
		for hour in (0, 8, 9, 13, 23):
			with self.subTest(hour=hour):
				now = self.at(2026, 5, 4, hour)

				self.assertEqual(
					self.next_run(now, date(2026, 5, 4)),
					self.at(2026, 5, 5, 9),
				)

	def test_a_missed_slot_runs_immediately(self):
		# The reason this is not a cron line. The machine was off at 09:00, the
		# points are still there, so the run happens as soon as it is back.
		now = self.at(2026, 5, 4, 14, 30)

		self.assertEqual(self.next_run(now, date(2026, 5, 3)), now)

	def test_a_machine_that_was_off_for_a_week_still_catches_up(self):
		now = self.at(2026, 5, 11, 14)

		self.assertEqual(self.next_run(now, date(2026, 5, 3)), now)

	def test_catch_up_can_be_turned_off(self):
		now = self.at(2026, 5, 4, 14, 30)

		self.assertEqual(
			self.next_run(now, date(2026, 5, 3), catch_up=False),
			self.at(2026, 5, 5, 9),
		)

	def test_a_run_that_has_never_happened_is_not_treated_as_todays(self):
		now = self.at(2026, 5, 4, 20)

		self.assertEqual(self.next_run(now, None), now)

	def test_a_day_that_has_used_its_attempts_waits_for_tomorrow(self):
		# Without this, catching up never stops: a day that fails every attempt
		# is still a day whose slot has passed with no success, so the answer
		# stays "run now" for the rest of it. An install with an expired sign-in
		# would start a browser continuously until somebody noticed.
		now = self.at(2026, 5, 4, 14)

		self.assertEqual(
			self.next_run(now, None, attempts=3, max_retries=2),
			self.at(2026, 5, 5, 9),
		)

	def test_attempts_left_still_run_now(self):
		now = self.at(2026, 5, 4, 14)

		for spent in (0, 1, 2):
			with self.subTest(attempts=spent):
				self.assertEqual(self.next_run(now, None, attempts=spent, max_retries=2), now)

	def test_yesterdays_attempts_do_not_count_against_today(self):
		self.assertEqual(scheduler.attempts_today({"attempts": {"date": "2026-05-03", "count": 3}}, date(2026, 5, 4)), 0)

	def test_todays_attempts_are_counted(self):
		self.assertEqual(scheduler.attempts_today({"attempts": {"date": "2026-05-04", "count": 3}}, date(2026, 5, 4)), 3)

	def test_a_missing_or_broken_counter_reads_as_none_spent(self):
		for state in ({}, {"attempts": None}, {"attempts": {}}, {"attempts": {"date": "2026-05-04", "count": "many"}}):
			with self.subTest(state=state):
				self.assertEqual(scheduler.attempts_today(state, date(2026, 5, 4)), 0)

	def test_the_slot_carries_the_jitter(self):
		schedule = self.schedule(jitter_minutes=90)
		now = self.at(2026, 5, 4, 7)
		target = scheduler.next_run(now, None, schedule, self.SEED)

		self.assertGreaterEqual(target, self.at(2026, 5, 4, 9))
		self.assertLessEqual(target, self.at(2026, 5, 4, 10, 30))


class TestLastSuccessDate(unittest.TestCase):
	"""The state file stores an instant; the schedule asks about a day."""

	def test_the_day_is_read_in_the_schedules_own_zone(self):
		# 23:30 UTC is already tomorrow in Berlin, and "has today been earned"
		# has to be answered on the calendar the schedule is written in.
		state = {"last_success": "2026-05-04T23:30:00+00:00"}

		self.assertEqual(
			scheduler.last_success_date(state, zone()),
			date(2026, 5, 5),
		)
		self.assertEqual(
			scheduler.last_success_date(state, timezone.utc),
			date(2026, 5, 4),
		)

	def test_a_missing_or_broken_value_is_no_date_rather_than_a_crash(self):
		for state in ({}, {"last_success": ""}, {"last_success": "yesterday"}, {"last_success": 5}):
			with self.subTest(state=state):
				self.assertIsNone(scheduler.last_success_date(state, zone()))

	def test_a_value_without_an_offset_is_read_as_utc(self):
		# Written by an older version, or edited by hand.
		self.assertEqual(
			scheduler.last_success_date({"last_success": "2026-05-04T23:30:00"}, timezone.utc),
			date(2026, 5, 4),
		)


class TestDaylightSaving(NextRunTestCase):
	"""The two days a year the configured time is not an ordinary instant."""

	def test_a_time_inside_the_spring_forward_gap_still_produces_a_run(self):
		# 02:30 does not exist in Berlin on 2026-03-29. A run at an odd time is
		# recoverable; a scheduler that raises here stops earning.
		schedule = self.schedule(hour=2, minute=30)
		now = datetime(2026, 3, 29, 0, 30, tzinfo=zone())
		target = scheduler.next_run(now, None, schedule, self.SEED)

		self.assertEqual(target.date(), date(2026, 3, 29))

	def test_a_time_inside_the_autumn_repeat_produces_one_run(self):
		schedule = self.schedule(hour=2, minute=30)
		now = datetime(2026, 10, 25, 0, 30, tzinfo=zone())
		target = scheduler.next_run(now, None, schedule, self.SEED)

		self.assertEqual(target.date(), date(2026, 10, 25))


class TestSleeping(unittest.TestCase):
	def test_a_target_in_the_past_does_not_wait(self):
		schedule = scheduler.Schedule(zone=timezone.utc)
		past = datetime.now(timezone.utc) - timedelta(minutes=5)

		self.assertTrue(scheduler._sleep_until(past, schedule, threading.Event()))

	def test_being_asked_to_stop_ends_the_wait(self):
		schedule = scheduler.Schedule(zone=timezone.utc)
		stop = threading.Event()
		stop.set()

		self.assertFalse(
			scheduler._sleep_until(
				datetime.now(timezone.utc) + timedelta(hours=1), schedule, stop
			)
		)


class StateTestCase(unittest.TestCase):
	"""Each case gets its own state directory, so none of them see each other."""

	def setUp(self):
		directory = tempfile.mkdtemp(prefix="rewards-state-")
		previous = os.environ.get(paths.STATE_DIR_ENV)

		os.environ[paths.STATE_DIR_ENV] = directory

		self.state_dir = directory

		self.addCleanup(_restore, paths.STATE_DIR_ENV, previous)


class TestRunState(StateTestCase):
	def test_no_state_file_reads_as_empty_rather_than_raising(self):
		self.assertEqual(run_state.read(), {})

	def test_a_successful_run_is_recorded(self):
		run_state.record_run_start()
		run_state.record_run_finish(0, 12.5, accounts_ran=2)

		state = run_state.read()

		self.assertEqual(state["last_exit_code"], 0)
		self.assertEqual(state["last_accounts_ran"], 2)
		self.assertEqual(state["consecutive_failures"], 0)
		self.assertFalse(state["running"])
		self.assertIn("last_success", state)

	def test_failures_are_counted_until_one_succeeds(self):
		for _ in range(3):
			run_state.record_run_finish(1, 1.0)

		self.assertEqual(run_state.read()["consecutive_failures"], 3)

		run_state.record_run_finish(0, 1.0, accounts_ran=1)

		self.assertEqual(run_state.read()["consecutive_failures"], 0)

	def test_a_failed_run_does_not_move_the_last_success(self):
		run_state.record_run_finish(0, 1.0, accounts_ran=1)
		succeeded = run_state.read()["last_success"]

		run_state.record_run_finish(1, 1.0)

		self.assertEqual(run_state.read()["last_success"], succeeded)

	def test_the_history_does_not_grow_without_limit(self):
		for _ in range(run_state.HISTORY_LIMIT + 10):
			run_state.record_run_finish(0, 1.0, accounts_ran=1)

		self.assertEqual(len(run_state.read()["history"]), run_state.HISTORY_LIMIT)

	def test_a_truncated_state_file_does_not_stop_the_next_run(self):
		# A power cut mid-write. The file is replaced rather than rewritten, so
		# this should not happen, but reading one must not raise either.
		with open(run_state.state_file(), "w", encoding="utf-8") as handle:
			handle.write('{"last_success": "2026-')

		self.assertEqual(run_state.read(), {})

		run_state.record_run_finish(0, 1.0, accounts_ran=1)

		self.assertEqual(run_state.read()["last_exit_code"], 0)

	def test_the_recorded_run_context_stores_what_the_run_reported(self):
		with run_state.recorded_run() as outcome:
			outcome["exit_code"] = 0
			outcome["accounts_ran"] = 3

		self.assertEqual(run_state.read()["last_accounts_ran"], 3)

	def test_a_crash_inside_a_recorded_run_is_recorded_as_a_failure(self):
		with self.assertRaises(RuntimeError):
			with run_state.recorded_run():
				raise RuntimeError("the browser died")

		state = run_state.read()

		self.assertEqual(state["last_exit_code"], 1)
		self.assertFalse(state["running"])

	def test_a_fresh_install_is_not_reported_as_stale(self):
		# Nothing has succeeded yet because nothing has run yet. Reporting that
		# as broken would make every new container unhealthy for a day.
		self.assertFalse(run_state.is_stale({}))

	def test_an_install_that_stopped_earning_is_stale(self):
		old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()

		self.assertTrue(run_state.is_stale({"last_success": old}))

	def test_a_recent_success_is_not_stale(self):
		recent = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()

		self.assertFalse(run_state.is_stale({"last_success": recent}))

	def test_the_lock_is_held_by_one_holder_at_a_time(self):
		with run_state.run_lock() as first:
			self.assertTrue(first)

		# Released at the end of the block, so the next run can take it.
		with run_state.run_lock() as second:
			self.assertTrue(second)


class TestAttemptBudget(StateTestCase):
	"""The day's attempts, kept in the file so a restart cannot reset them."""

	def test_attempts_accumulate_across_processes(self):
		day = date(2026, 5, 4)

		for expected in (1, 2, 3):
			scheduler.record_attempt(day)

			self.assertEqual(scheduler.attempts_today(run_state.read(), day), expected)

	def test_a_new_day_starts_the_budget_again(self):
		scheduler.record_attempt(date(2026, 5, 4))
		scheduler.record_attempt(date(2026, 5, 5))

		self.assertEqual(scheduler.attempts_today(run_state.read(), date(2026, 5, 5)), 1)


class TestInterruptedRuns(StateTestCase):
	"""A run that was killed before it could record its own outcome.

	The timeout the scheduler enforces, an OOM kill, the host losing power.
	Without this the state file reads "a run is in progress" forever, which
	makes --status lie and hides every later failure behind it.
	"""

	def test_a_run_left_in_progress_is_closed_out(self):
		run_state.record_run_start()

		self.assertTrue(run_state.reconcile_interrupted(scheduler.TIMEOUT_EXIT_CODE, 7200))

		state = run_state.read()

		self.assertFalse(state["running"])
		self.assertEqual(state["last_exit_code"], scheduler.TIMEOUT_EXIT_CODE)
		self.assertEqual(state["consecutive_failures"], 1)

	def test_a_finished_run_is_left_alone(self):
		run_state.record_run_start()
		run_state.record_run_finish(0, 12.0, accounts_ran=1)

		self.assertFalse(run_state.reconcile_interrupted(1, 0))
		self.assertEqual(run_state.read()["last_exit_code"], 0)

	def test_nothing_recorded_at_all_is_not_an_interrupted_run(self):
		self.assertFalse(run_state.reconcile_interrupted())


class TestInstallSeed(StateTestCase):
	def test_the_seed_is_created_once_and_then_reused(self):
		first = scheduler.install_seed()

		self.assertTrue(first)
		self.assertEqual(scheduler.install_seed(), first)

	def test_two_installs_get_different_seeds(self):
		first = scheduler.install_seed()

		with tempfile.TemporaryDirectory() as other:
			os.environ[paths.STATE_DIR_ENV] = other

			self.assertNotEqual(scheduler.install_seed(), first)


class TestDailyLoop(StateTestCase):
	"""One simulated day through loop(), with a fake clock and no browser.

	The clock is fake in three places at once - the schedule's own now(), the
	waiting, and the timestamps the state file records - because they have to
	agree: a run recorded at the real time while the loop thinks it is May
	would leave "has today been earned" answered against the wrong day.

	The loop is stopped the moment it schedules something for tomorrow, so what
	each case asserts is exactly what one day did.
	"""

	DAY = date(2026, 5, 4)

	def setUp(self):
		super().setUp()

		self.clock = datetime(2026, 5, 4, 8, 0, tzinfo=timezone.utc)
		self.runs: list[datetime] = []
		self.waits: list[datetime] = []
		self.stop = threading.Event()

		logging.disable(logging.CRITICAL)
		self.addCleanup(logging.disable, logging.NOTSET)

		schedule_now = scheduler.Schedule.now
		self.addCleanup(setattr, scheduler.Schedule, "now", schedule_now)
		scheduler.Schedule.now = lambda _schedule: self.clock

		real_sleep = scheduler._sleep_until
		self.addCleanup(setattr, scheduler, "_sleep_until", real_sleep)
		scheduler._sleep_until = self._sleep_until

		real_now = run_state._now
		self.addCleanup(setattr, run_state, "_now", real_now)
		run_state._now = lambda: self.clock.isoformat(timespec="seconds")

	def _sleep_until(self, target, _schedule, stop):
		self.waits.append(target)

		# Tomorrow's run is where this test ends: one day is the unit under
		# test, and the loop would otherwise never return.
		if target.date() > self.DAY:
			stop.set()

			return False

		if stop.is_set():
			return False

		self.clock = max(self.clock, target)

		return True

	def run_loop(self, outcomes, max_retries=2):
		"""Drive one day, with `outcomes` as the exit code of each run."""
		remaining = list(outcomes)
		test = self

		class FakeRunner:
			def run(self):
				test.runs.append(test.clock)

				code = remaining.pop(0) if remaining else 1

				# What main.py records for itself, in the same fake time.
				run_state.record_run_start()
				run_state.record_run_finish(code, 60.0, accounts_ran=1 if code == 0 else 0)

				# A run takes time, and a retry scheduled from the moment it
				# started rather than the moment it ended would be early.
				test.clock += timedelta(minutes=10)

				return code

			def stop(self):
				pass

		schedule = scheduler.Schedule(
			hour=9,
			minute=0,
			jitter_minutes=0,
			retry_minutes=30,
			max_retries=max_retries,
			zone=timezone.utc,
		)

		scheduler.loop(schedule, self.stop, FakeRunner())

	def test_a_good_day_is_one_run(self):
		self.run_loop([0])

		self.assertEqual(len(self.runs), 1)
		self.assertEqual(self.runs[0], datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc))
		# And the next thing it waits for is tomorrow, not another go at today.
		self.assertEqual(self.waits[-1], datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc))

	def test_a_failed_run_is_retried_and_then_left_alone(self):
		self.run_loop([1, 1, 0])

		self.assertEqual(len(self.runs), 3)
		# 09:00, then a ten minute run and a thirty minute wait, twice.
		self.assertEqual(
			[run.strftime("%H:%M") for run in self.runs],
			["09:00", "09:40", "10:20"],
		)
		self.assertEqual(run_state.read()["last_exit_code"], 0)

	def test_a_day_that_never_succeeds_stops_after_its_attempts(self):
		# The case that would otherwise start a browser continuously: every run
		# fails, so the slot has passed with no success all day long.
		self.run_loop([1, 1, 1, 1, 1, 1])

		self.assertEqual(len(self.runs), 3)
		self.assertEqual(self.waits[-1].date(), date(2026, 5, 5))

	def test_the_budget_is_not_reset_by_a_restart(self):
		self.run_loop([1, 1, 1])

		self.assertEqual(len(self.runs), 3)

		# The container comes back an hour later. The day has had its attempts,
		# so it waits for tomorrow rather than starting again from zero.
		self.stop = threading.Event()
		self.clock = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)

		self.run_loop([0])

		self.assertEqual(len(self.runs), 3)

	def test_a_restart_after_a_good_run_does_not_run_the_day_twice(self):
		self.run_loop([0])

		self.stop = threading.Event()
		self.clock = datetime(2026, 5, 4, 18, 0, tzinfo=timezone.utc)

		self.run_loop([0])

		self.assertEqual(len(self.runs), 1)

	def test_a_missed_slot_runs_as_soon_as_the_machine_is_back(self):
		# Started at 14:00 with nothing recorded: the machine was off.
		self.clock = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)

		self.run_loop([0])

		self.assertEqual(len(self.runs), 1)
		self.assertEqual(self.runs[0], datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc))


class TestPaths(EnvironmentTestCase):
	def test_paths_are_anchored_to_the_repository_not_the_working_directory(self):
		# The bug this module exists for: started from anywhere else, a run
		# created a new empty profile beside wherever it was launched from,
		# signed in to nobody, and reported success.
		previous = os.getcwd()
		self.addCleanup(os.chdir, previous)

		before = paths.data_dir()

		os.chdir(tempfile.gettempdir())

		self.assertEqual(paths.data_dir(), before)
		self.assertTrue(paths.data_dir().is_absolute())

	def test_every_path_can_be_moved_with_an_environment_variable(self):
		cases = (
			(paths.DATA_DIR_ENV, paths.data_dir),
			(paths.STATE_DIR_ENV, paths.state_dir),
			(paths.NOUNS_ENV, paths.nouns_file),
			(paths.VISUAL_SEARCH_ENV, paths.visual_search_image),
		)

		for variable, getter in cases:
			with self.subTest(variable=variable):
				os.environ[variable] = "/srv/rewards/somewhere"

				self.assertEqual(str(getter()), "/srv/rewards/somewhere")

	def test_a_relative_override_is_taken_against_the_repository(self):
		os.environ[paths.DATA_DIR_ENV] = "profiles"

		self.assertEqual(paths.data_dir(), paths.REPO_ROOT / "profiles")

	def test_the_defaults_are_where_they_have_always_been(self):
		self.assertEqual(paths.data_dir(), paths.REPO_ROOT / "data-dir")
		self.assertEqual(paths.nouns_file(), paths.REPO_ROOT / "nouns.txt")
		self.assertEqual(paths.visual_search_image(), paths.REPO_ROOT / "visual_search.jpg")

	def test_the_state_directory_is_not_under_the_profiles(self):
		# Account names become directories under data-dir. Anything else living
		# there is one unlucky account name away from a collision.
		self.assertNotEqual(paths.state_dir().parent, paths.data_dir())

	def test_the_wordlist_is_found_from_another_directory(self):
		import query_sources

		previous = os.getcwd()
		self.addCleanup(os.chdir, previous)

		os.chdir(tempfile.gettempdir())

		self.assertTrue(query_sources.wordlist_queries(3))


if __name__ == "__main__":
	unittest.main()
