import logging
import os
import socket
import sys
from pathlib import Path

import log_utils
import accounts
import paths
import rewards_tasks
import run_state
from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException

HEADLESS = os.environ.get("REWARDS_HEADLESS", "").strip().lower() in ("1", "true", "yes")

# Exit code for "another run holds the profile". Distinct from a failed run,
# because a scheduler retrying this one would only collide again.
BUSY_EXIT_CODE = 3

logger = logging.getLogger(__name__)


def _interactive() -> bool:
	"""Whether there is a human at the other end to press Enter.

	A scheduled run has no terminal: cron gives it a pipe, systemd gives it the
	journal, and a container gives it nothing at all. Waiting for a keypress
	there is a run that never ends, holding the profile against every run after
	it, so the prompt is only shown when stdin really is a terminal.
	"""
	if os.environ.get("REWARDS_SCHEDULED", "").strip():
		return False

	try:
		return sys.stdin is not None and sys.stdin.isatty()
	except (AttributeError, ValueError):
		return False


def build_options(account: accounts.Account) -> webdriver.EdgeOptions:
	options = webdriver.EdgeOptions()

	options.add_experimental_option("excludeSwitches", ["enable-automation"])
	options.add_experimental_option('useAutomationExtension', False)
	options.add_argument("--disable-blink-features=AutomationControlled")
	options.add_argument(f"--user-data-dir={account.user_data_dir}")
	options.add_argument(f"--profile-directory={account.profile_name}")

	if HEADLESS:
		# A container has no display. The window size is set explicitly because
		# the pointer code works in viewport coordinates, and the default
		# headless window is small enough to put cards out of reach.
		options.add_argument("--headless=new")
		options.add_argument("--window-size=1920,1080")
		options.add_argument("--no-sandbox")
		options.add_argument("--disable-dev-shm-usage")

	return options


def clear_stale_profile_lock(account: accounts.Account) -> bool:
	"""Remove a profile lock left behind by a browser that was killed.

	Chromium marks a profile as in use with a `SingletonLock` symlink whose
	target is `hostname-pid`. It is removed on a clean exit and survives
	anything else: a power cut, an OOM kill, `docker kill`. The next run then
	finds the profile claimed by a process that no longer exists and exits
	during startup, and it keeps doing that until a human deletes the file. On
	an unattended install that is every run from then on.

	Only a lock this machine wrote, naming a process that is gone, is removed.
	A lock from another host is left alone, because from here it is
	indistinguishable from a profile genuinely open on a machine that shares the
	directory - unless REWARDS_FORCE_PROFILE_UNLOCK says otherwise, which is for
	the case where the hostname changes every start and nothing else can hold
	the profile anyway.
	"""
	directory = Path(account.user_data_dir)
	lock = directory / "SingletonLock"

	try:
		target = os.readlink(lock)
	except OSError:
		# No lock, or a platform where it is a real file rather than a symlink.
		# Windows is the latter, and there the running browser holds the file
		# open, so deleting it is neither possible nor needed.
		return False

	host, _, pid = target.rpartition("-")
	forced = os.environ.get("REWARDS_FORCE_PROFILE_UNLOCK", "").strip().lower() in ("1", "true", "yes")

	if host != socket.gethostname() and not forced:
		logger.warning(
			"%s: profile claimed by %s, leaving it alone. If that machine is gone, "
			"set REWARDS_FORCE_PROFILE_UNLOCK=1.",
			account.name, target
		)

		return False

	if host == socket.gethostname() and _process_alive(pid):
		# A real browser on this machine has it. run_account reports that.
		return False

	logger.warning("%s: clearing a stale profile lock left by %s", account.name, target)

	for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
		try:
			(directory / name).unlink()
		except OSError:
			pass

	return True


def _process_alive(pid: str) -> bool:
	"""Whether a pid from a profile lock is still running.

	Unparseable or unknown counts as alive: refusing to start is recoverable,
	starting a second browser on a profile that is genuinely open is not.
	"""
	try:
		number = int(pid)
	except ValueError:
		return True

	if number <= 0:
		return False

	try:
		os.kill(number, 0)
	except ProcessLookupError:
		return False
	except PermissionError:
		# Running, owned by somebody else.
		return True
	except OSError:
		return True

	return True


def run_account(account: accounts.Account) -> bool:
	"""Work one account. Returns whether the browser started."""
	clear_stale_profile_lock(account)

	try:
		driver = webdriver.Edge(options=build_options(account))
	except SessionNotCreatedException as exc:
		# Chromium allows one process per user data directory. When the profile
		# is already open the driver's copy exits during startup, and selenium
		# reports it as the browser crashing with a message that names neither
		# the profile nor the other window.
		logger.error("[FAIL] %s: could not start Edge with this profile.", account.name)
		logger.error("       profile directory: %s", account.user_data_dir)
		logger.error("       The usual cause is that this profile is already open in another")
		logger.error("       Edge window, including one left over from a previous run.")
		logger.error("       driver said: %s", log_utils.exception_summary(exc))

		return False

	try:
		rewards = rewards_tasks.RewardsTaskUtils(driver)
		rewards.complete_all_tasks()
	finally:
		try:
			driver.quit()
		except Exception as exc:
			# quit() raises when the browser is already gone. Letting it out
			# here would replace whatever actually went wrong with the tidy-up's
			# own error, and the process it is meant to end is dead anyway.
			logger.warning(
				"%s: the driver did not shut down cleanly: %s",
				account.name, log_utils.exception_summary(exc)
			)

	return True


def run_all(configured: list[accounts.Account]) -> int:
	"""Work every configured account. Returns how many browsers started."""
	started = 0

	for account in configured:
		if len(configured) > 1:
			logger.info("=== account: %s ===", account.name)

		# One account must not be able to end the batch. complete_all_tasks
		# already contains a task that fails, and run_account names the profile
		# that is already open, but everything else - a driver that will not
		# start for some other reason, the browser dying mid-run, a page that
		# never loads - reached here and took the remaining accounts with it.
		# KeyboardInterrupt is deliberately not caught: Ctrl-C means stop.
		try:
			if run_account(account):
				started += 1
		except Exception as exc:
			logger.error(
				"[FAIL] %s: %s: %s",
				account.name, type(exc).__name__, log_utils.exception_summary(exc),
				exc_info=logger.isEnabledFor(logging.DEBUG)
			)

	if len(configured) > 1:
		logger.info("%s/%s accounts ran", started, len(configured))

	return started


def main() -> int:
	log_utils.setup_logging()

	# Which directories this run is actually using. The answer used to depend on
	# where the process was started from, so it is worth having in the log of an
	# unattended run that earned nothing.
	logger.debug("Profiles: %s", paths.data_dir())
	logger.debug("State:    %s", run_state.state_file())

	try:
		configured = accounts.configured()
	except ValueError as exc:
		logger.error("[FAIL] %s", exc)

		return 2

	# Chromium allows one process per profile directory, so an overlapping run
	# does not produce two runs, it produces one run and one browser that exits
	# during startup. Refusing here says so plainly; without it the second run
	# reports a crashed browser and looks like a bug in this project.
	with run_state.run_lock() as acquired:
		if not acquired:
			logger.error("[FAIL] another run is already in progress (%s)", run_state.lock_file())
			logger.error("       Wait for it to finish, or stop it, and try again.")

			return BUSY_EXIT_CODE

		with run_state.recorded_run() as outcome:
			started = run_all(configured)

			outcome["accounts_ran"] = started
			outcome["exit_code"] = 0 if started else 1

		# After the state is written, so a human reading it is looking at the
		# finished record rather than a run still marked as in progress.
		if _interactive():
			input("Press Enter to exit...")

		return 0 if started else 1


if __name__ == "__main__":
	sys.exit(main())
