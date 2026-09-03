"""Tests for running more than one account in a single run.

Names become directory names, so most of these are about refusing one that
would resolve somewhere other than where it reads: `..`, an absolute path, and
the trailing dot Win32 strips but Python's normalisation does not. The rest
cover the run loop, where one account failing used to end the batch and take
the remaining accounts with it.

None of these need a browser. The last case does, and starts Edge twice to show
that two profiles hold two independent, persistent identities, so it is opt in:

	python -m unittest discover -s tests
	REWARDS_BROWSER_TESTS=1 python -m unittest discover -s tests
"""

import logging
import os
import shutil
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from selenium.common.exceptions import (
	NoSuchDriverException,
	SessionNotCreatedException,
	WebDriverException,
)

import accounts
import main
import paths
import run_state
from constants import USER_DATA_DIR

# Names that have to be refused, with the reason each one is not simply a
# directory sitting under data-dir.
REFUSED_NAMES = [
	# relative traversal
	"..", ".", "../escape", "..\\escape", "a/b", "a\\b",
	# absolute, drive relative and UNC
	"/etc", "\\", "/", "C:", "C:\\Windows", "\\\\server\\share",
	# expanded by a shell somewhere else, not here
	"~", "%TEMP%", "$HOME",
	# Win32 strips a trailing dot from a path component and Python does not, so
	# "personal." is the "personal" directory and "..." is data-dir itself
	"...", "....", "personal.", "personal..",
	# shell and filesystem metacharacters
	"a b", "a:b", "a;b", "a|b", "a*b", "a?b", "a<b",
]


def accounts_for(value):
	"""configured() under a given REWARDS_ACCOUNTS, or ValueError."""
	if value is None:
		os.environ.pop(accounts.ENV_VAR, None)
	else:
		os.environ[accounts.ENV_VAR] = value

	return accounts.configured()


class EnvironmentTestCase(unittest.TestCase):
	"""Restores everything these tests reach into, so ordering cannot matter."""

	def setUp(self):
		self.addCleanup(os.environ.pop, accounts.ENV_VAR, None)

		# main() records what it did, and a suite that leaves that record in the
		# checkout would also read a real install's counters when run on one.
		state_dir = tempfile.mkdtemp(prefix="rewards-test-state-")
		previous = os.environ.get(paths.STATE_DIR_ENV)

		os.environ[paths.STATE_DIR_ENV] = state_dir

		self.addCleanup(shutil.rmtree, state_dir, True)
		self.addCleanup(_restore_env, paths.STATE_DIR_ENV, previous)


def _restore_env(variable, value):
	if value is None:
		os.environ.pop(variable, None)
	else:
		os.environ[variable] = value


class TestAccountConfiguration(EnvironmentTestCase):
	def test_unset_is_the_existing_single_profile(self):
		configured = accounts_for(None)

		self.assertEqual([a.name for a in configured], ["default"])
		self.assertEqual(configured[0].user_data_dir, USER_DATA_DIR)
		self.assertTrue(configured[0].is_default)

	def test_blank_falls_back_to_the_single_profile(self):
		self.assertEqual([a.name for a in accounts_for("   ")], ["default"])

	def test_names_are_taken_in_order(self):
		self.assertEqual([a.name for a in accounts_for("personal,spare")], ["personal", "spare"])

	def test_surrounding_whitespace_and_empty_entries_are_ignored(self):
		self.assertEqual([a.name for a in accounts_for(" personal , spare ")], ["personal", "spare"])
		self.assertEqual([a.name for a in accounts_for("personal,,spare,")], ["personal", "spare"])

	def test_duplicates_collapse_case_insensitively(self):
		# Running the same profile twice earns nothing the second time and
		# doubles the length of the run.
		self.assertEqual(
			[a.name for a in accounts_for("personal,PERSONAL,spare")], ["personal", "spare"]
		)

	def test_unusable_directory_names_are_refused(self):
		for name in REFUSED_NAMES:
			with self.subTest(name=name):
				with self.assertRaises(ValueError):
					accounts_for(f"good,{name}")

	def test_a_leading_dot_is_still_usable(self):
		# Only the trailing dot moves the directory.
		self.assertEqual([a.name for a in accounts_for(".hidden")], [".hidden"])

	def test_each_account_gets_its_own_directory(self):
		named = accounts_for("personal,spare")

		self.assertEqual(len({a.user_data_dir for a in named}), 2)
		# Distinct strings are not enough: two names can spell one directory,
		# which is what the trailing dot did, so compare where they land.
		self.assertEqual(len({os.path.realpath(a.user_data_dir) for a in named}), 2)

	def test_every_directory_sits_under_the_profile_directory(self):
		root = os.path.realpath(USER_DATA_DIR)

		for account in accounts_for("personal,spare"):
			with self.subTest(account=account.name):
				resolved = os.path.realpath(account.user_data_dir)

				self.assertEqual(os.path.commonpath([root, resolved]), root)
				self.assertNotEqual(resolved, root)
				self.assertFalse(account.is_default)


class TestEdgeOptions(EnvironmentTestCase):
	def test_each_account_is_handed_its_own_profile(self):
		seen = []

		for account in accounts_for("personal,spare"):
			arguments = main.build_options(account).arguments
			user_data = [a for a in arguments if a.startswith("--user-data-dir=")]
			profile = [a for a in arguments if a.startswith("--profile-directory=")]

			with self.subTest(account=account.name):
				self.assertEqual(len(user_data), 1)
				self.assertEqual(len(profile), 1)

			seen.append(user_data[0])

		self.assertEqual(len(set(seen)), 2)


class RunLoopTestCase(EnvironmentTestCase):
	"""main() drives real browsers, so both entry points are replaced here."""

	def setUp(self):
		super().setUp()

		# main() waits on input() when it is not headless, which would hang.
		headless = main.HEADLESS
		main.HEADLESS = True
		self.addCleanup(setattr, main, "HEADLESS", headless)

		# main() reports per account at info, and the failure paths at error, by
		# design. The assertions are what reports the outcome here, so keep the
		# suite's own output to what unittest prints.
		logging.disable(logging.CRITICAL)
		self.addCleanup(logging.disable, logging.NOTSET)


class TestRunLoop(RunLoopTestCase):
	def setUp(self):
		super().setUp()

		self.calls = []
		real = main.run_account
		self.addCleanup(setattr, main, "run_account", real)

	def _record(self, started):
		def run_account(account):
			self.calls.append(account.name)

			return started(account.name)

		main.run_account = run_account

	def test_a_profile_that_fails_to_start_does_not_end_the_run(self):
		accounts_for("personal,spare")
		self._record(lambda name: name != "personal")

		self.assertEqual(main.main(), 0)
		self.assertEqual(self.calls, ["personal", "spare"])

	def test_exit_code_is_one_when_no_account_ran(self):
		accounts_for("personal,spare")
		self._record(lambda name: False)

		self.assertEqual(main.main(), 1)
		self.assertEqual(self.calls, ["personal", "spare"])

	def test_unset_still_runs_the_single_profile(self):
		accounts_for(None)
		self._record(lambda name: True)

		self.assertEqual(main.main(), 0)
		self.assertEqual(self.calls, ["default"])


class TestFailureIsolation(RunLoopTestCase):
	"""One account failing, every way it can, without ending the batch.

	Only selenium is replaced: run_account and main() are the shipped ones, so
	what is measured is where their exception handling actually reaches. Before
	this, only "profile already open" was handled by name and everything else
	took the remaining accounts with it.
	"""

	# (label, where it fails, the exception raised)
	CASES = [
		("the profile is already open", "start", SessionNotCreatedException("profile in use")),
		("the driver will not start", "start", WebDriverException("driver version mismatch")),
		("there is no driver installed", "start", NoSuchDriverException("msedgedriver not found")),
		("the profile directory is unwritable", "start", PermissionError(13, "Permission denied")),
		("the first page never loads", "connect", WebDriverException("net::ERR_NAME_NOT_RESOLVED")),
		("the browser dies mid-run", "tasks", WebDriverException("chrome not reachable")),
		("the browser will not shut down", "quit", WebDriverException("browser already gone")),
	]

	def setUp(self):
		super().setUp()

		edge, tasks = main.webdriver.Edge, main.rewards_tasks.RewardsTaskUtils
		self.addCleanup(setattr, main.webdriver, "Edge", edge)
		self.addCleanup(setattr, main.rewards_tasks, "RewardsTaskUtils", tasks)

	def _install(self, fail_at, exc, started, quit_cleanly):
		def account_of(options):
			flag = next(a for a in options.arguments if a.startswith("--user-data-dir="))

			return os.path.basename(flag.split("=", 1)[1])

		class Driver:
			def __init__(self, options):
				self.name = account_of(options)
				started.append(self.name)

				if self.name == "two" and fail_at == "start":
					raise exc

			def quit(self):
				if self.name == "two" and fail_at == "quit":
					raise exc

				quit_cleanly.append(self.name)

		class Tasks:
			def __init__(self, driver):
				self.driver = driver

				if driver.name == "two" and fail_at == "connect":
					raise exc

			def complete_all_tasks(self):
				if self.driver.name == "two" and fail_at == "tasks":
					raise exc

		main.webdriver.Edge = Driver
		main.rewards_tasks.RewardsTaskUtils = Tasks

	def test_the_remaining_accounts_still_run(self):
		for label, fail_at, exc in self.CASES:
			with self.subTest(case=label):
				started, quit_cleanly = [], []
				self._install(fail_at, exc, started, quit_cleanly)
				accounts_for("one,two,three")

				self.assertEqual(main.main(), 0)
				self.assertEqual(started, ["one", "two", "three"])

				# Whatever went wrong, a browser that started has to be shut
				# down or its profile stays locked against the next run.
				expected = ["one", "three"] if fail_at in ("start", "quit") else ["one", "two", "three"]
				self.assertEqual(quit_cleanly, expected)

	def test_a_lone_accounts_failure_is_still_a_failing_exit_code(self):
		# The unset, unchanged path. Its failure has nothing left to protect, so
		# it must not be swallowed into a success.
		accounts_for(None)
		real = main.run_account
		self.addCleanup(setattr, main, "run_account", real)

		def boom(_):
			raise WebDriverException("chrome not reachable")

		main.run_account = boom

		self.assertEqual(main.main(), 1)


class TestOverlappingRuns(RunLoopTestCase):
	"""A second run while one is in progress, which a schedule makes possible.

	A timer that fires while yesterday's run is somehow still going, or a human
	starting one by hand at the wrong moment. Chromium allows one process per
	profile directory, so the second browser exits during startup and the run
	reports a crash; refusing up front says what actually happened.
	"""

	def test_a_second_run_refuses_instead_of_starting_a_browser(self):
		started = []

		real = main.run_account
		self.addCleanup(setattr, main, "run_account", real)

		main.run_account = lambda account: started.append(account.name) or True

		with run_state.run_lock() as held:
			self.assertTrue(held)

			self.assertEqual(main.main(), main.BUSY_EXIT_CODE)

		self.assertEqual(started, [])

	def test_a_run_records_what_it_did(self):
		real = main.run_account
		self.addCleanup(setattr, main, "run_account", real)
		main.run_account = lambda account: True

		self.assertEqual(main.main(), 0)

		state = run_state.read()

		self.assertEqual(state["last_exit_code"], 0)
		self.assertEqual(state["last_accounts_ran"], 1)
		self.assertFalse(state["running"])


class TestStaleProfileLock(EnvironmentTestCase):
	"""A profile lock left behind by a browser that was killed.

	Chromium writes `SingletonLock` as a symlink to `hostname-pid` and removes
	it on a clean exit only. After a power cut or an OOM kill it names a process
	that no longer exists, and every run from then on exits during startup - on
	an unattended install, every run for as long as nobody looks.
	"""

	def setUp(self):
		super().setUp()

		self.directory = tempfile.mkdtemp(prefix="rewards-test-profile-")
		self.addCleanup(shutil.rmtree, self.directory, True)

		self.account = accounts.Account(
			name="test", user_data_dir=self.directory, profile_name="Default"
		)
		self.lock = os.path.join(self.directory, "SingletonLock")

		self.addCleanup(os.environ.pop, "REWARDS_FORCE_PROFILE_UNLOCK", None)

		logging.disable(logging.CRITICAL)
		self.addCleanup(logging.disable, logging.NOTSET)

	def write_lock(self, target):
		os.symlink(target, self.lock)

	def test_a_lock_from_a_dead_process_on_this_machine_is_cleared(self):
		# A pid that cannot be running: the kernel would have to have wrapped
		# all the way around and landed on it, and it is not this process.
		self.write_lock(f"{socket.gethostname()}-4194303")

		self.assertTrue(main.clear_stale_profile_lock(self.account))
		self.assertFalse(os.path.lexists(self.lock))

	def test_a_lock_held_by_a_live_process_is_left_alone(self):
		self.write_lock(f"{socket.gethostname()}-{os.getpid()}")

		self.assertFalse(main.clear_stale_profile_lock(self.account))
		self.assertTrue(os.path.lexists(self.lock))

	def test_a_lock_from_another_machine_is_left_alone(self):
		# Indistinguishable from a profile genuinely open on another machine
		# that shares the directory, and clearing that one corrupts it.
		self.write_lock("some-other-host-4194303")

		self.assertFalse(main.clear_stale_profile_lock(self.account))
		self.assertTrue(os.path.lexists(self.lock))

	def test_another_machines_lock_can_be_cleared_deliberately(self):
		self.write_lock("some-other-host-4194303")

		os.environ["REWARDS_FORCE_PROFILE_UNLOCK"] = "1"

		self.assertTrue(main.clear_stale_profile_lock(self.account))
		self.assertFalse(os.path.lexists(self.lock))

	def test_an_unparseable_lock_is_treated_as_held(self):
		# Refusing to start is recoverable. Starting a second browser against a
		# profile that is genuinely open is not.
		self.write_lock("nonsense")

		self.assertFalse(main.clear_stale_profile_lock(self.account))
		self.assertTrue(os.path.lexists(self.lock))

	def test_no_lock_is_not_an_error(self):
		self.assertFalse(main.clear_stale_profile_lock(self.account))

	def test_the_companion_files_go_too(self):
		# Chromium checks all three, so removing only the one it names leaves
		# the profile just as unusable.
		self.write_lock(f"{socket.gethostname()}-4194303")

		for name in ("SingletonCookie", "SingletonSocket"):
			os.symlink("whatever", os.path.join(self.directory, name))

		self.assertTrue(main.clear_stale_profile_lock(self.account))

		for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
			self.assertFalse(os.path.lexists(os.path.join(self.directory, name)))


@unittest.skipUnless(
	os.environ.get("REWARDS_BROWSER_TESTS"),
	"starts Edge twice; set REWARDS_BROWSER_TESTS=1 to run",
)
class TestTwoRealProfiles(EnvironmentTestCase):
	"""Two profiles, two independent identities, each surviving a restart.

	Keyed on bing.com's own MUID rather than an injected cookie: one added
	through webdriver is not written to the profile the way a Set-Cookie is, so
	it would prove nothing about a sign-in outliving the browser.
	"""

	def setUp(self):
		super().setUp()

		self.first, self.second = accounts_for("mat_a,mat_b")

		for account in (self.first, self.second):
			self.addCleanup(shutil.rmtree, account.user_data_dir, ignore_errors=True)

	def identity_of(self, account):
		from selenium import webdriver

		driver = webdriver.Edge(options=main.build_options(account))

		try:
			driver.get("https://www.bing.com")
			cookie = driver.get_cookie("MUID")

			return cookie["value"] if cookie else None
		finally:
			driver.quit()

	def test_two_profiles_hold_two_persistent_identities(self):
		first_id = self.identity_of(self.first)
		second_id = self.identity_of(self.second)

		self.assertIsNotNone(first_id)
		self.assertIsNotNone(second_id)
		self.assertNotEqual(first_id, second_id)

		# The point of a profile per account: the identity has to outlive the
		# browser, or a sign-in would not either.
		self.assertEqual(self.identity_of(self.first), first_id)
		self.assertEqual(self.identity_of(self.second), second_id)

		for account in (self.first, self.second):
			self.assertTrue(os.path.isdir(account.user_data_dir))

		self.assertEqual(
			len({os.path.realpath(a.user_data_dir) for a in (self.first, self.second)}), 2
		)


if __name__ == "__main__":
	unittest.main()
