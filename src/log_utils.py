"""Central logging configuration.

`setup_logging` is called once from `main.py`. Every other module just does
`logger = logging.getLogger(__name__)` at import time, which is safe to do
before this runs, so import order does not matter.
"""

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

LEVEL_ENV_VAR = "REWARDS_FARMER_LOG_LEVEL"
FILE_ENV_VAR = "REWARDS_FARMER_LOG_FILE"
MAX_BYTES_ENV_VAR = "REWARDS_FARMER_LOG_MAX_BYTES"
BACKUPS_ENV_VAR = "REWARDS_FARMER_LOG_BACKUPS"

DEFAULT_LEVEL = "INFO"

# A run writes a few hundred lines, and a scheduled install writes them every
# day for as long as the machine is up. The file used to grow without limit,
# which on a home server is a slow leak rather than an obvious failure: 5 MB
# per file and four of them keeps roughly a season of runs in 25 MB.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUPS = 4

# Configuring the root logger switches on output for every library that logs,
# not just ours. httpx emits an info line per ollama call, which buries the
# task summary and puts the ollama endpoint in the log file. print never did
# this because it never touched logging at all, so leaving these at their
# default would make the output noisier than what it replaces.
NOISY_LIBRARIES = ("httpx", "httpcore", "urllib3", "selenium")

# Longest a one-line exception summary may get before it is cut. Long enough
# for any real selenium message, short enough that a pathological one cannot
# push a whole screen of text into a single record.
MAX_SUMMARY_LENGTH = 300

_SESSION_INFO = re.compile(r"\s*\(Session info:[^)]*\)")


def exception_summary(exc: BaseException) -> str:
	"""One short line describing an exception, safe to put in a log record.

	str() on a selenium exception is multi-line: the message, then a session
	info line, then the whole msedgedriver stacktrace. Only the first line is
	worth showing in a per-task summary, and the full detail is still attached
	as a traceback when the level is debug.
	"""
	text = str(exc).strip()

	if not text:
		return ""

	text = _SESSION_INFO.sub("", text.splitlines()[0]).strip()

	if len(text) > MAX_SUMMARY_LENGTH:
		# ASCII, because this can land on a Windows console whose encoding
		# cannot represent an ellipsis character.
		text = text[:MAX_SUMMARY_LENGTH - 3].rstrip() + "..."

	return text

# CRITICAL is the longest level name at 8 characters, so pad to that and the
# message column stays aligned no matter what is being logged.
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"

_configured = False


def _resolve_level(level: str | int | None) -> int:
	"""Turn a level name, a level number or None into a level number.

	An unusable value falls back to the default rather than raising. A typo in
	an environment variable must not be able to take down an unattended run.
	"""
	if level is None:
		level = os.environ.get(LEVEL_ENV_VAR, DEFAULT_LEVEL)

	if isinstance(level, int):
		return level

	resolved = logging.getLevelNamesMapping().get(str(level).strip().upper())

	if resolved is None:
		logging.getLogger(__name__).warning(
			"Unknown log level %r, falling back to %s", level, DEFAULT_LEVEL
		)

		return logging.getLevelNamesMapping()[DEFAULT_LEVEL]

	return resolved


def _positive_int(variable: str, default: int) -> int:
	raw = os.environ.get(variable, "").strip()

	if not raw:
		return default

	try:
		value = int(raw)
	except ValueError:
		return default

	return value if value >= 0 else default


def _file_handler(log_file: str) -> logging.Handler | None:
	"""A rotating handler for `log_file`, or None if it cannot be opened.

	A log file that cannot be written is a reason to log to the console only,
	not a reason to fail the run: the directory may be missing on a fresh
	install, or read-only on a volume that was mounted wrong, and neither
	should cost a day's points.
	"""
	path = Path(log_file).expanduser()

	try:
		if path.parent and not path.parent.exists():
			path.parent.mkdir(parents=True, exist_ok=True)

		# utf-8 explicitly. Card descriptions are scraped from the page and are
		# not ASCII outside the en-US market, and the Windows default encoding
		# would raise on them.
		return logging.handlers.RotatingFileHandler(
			path,
			maxBytes=_positive_int(MAX_BYTES_ENV_VAR, DEFAULT_MAX_BYTES),
			backupCount=_positive_int(BACKUPS_ENV_VAR, DEFAULT_BACKUPS),
			encoding="utf-8",
		)
	except OSError as exc:
		logging.getLogger(__name__).warning("Could not open log file %s: %s", path, exc)

		return None


def setup_logging(level: str | int | None = None, log_file: str | None = None) -> None:
	"""Configure the root logger. Calling this more than once is a no-op.

	`level` defaults to $REWARDS_FARMER_LOG_LEVEL, then to INFO.
	`log_file` defaults to $REWARDS_FARMER_LOG_FILE, and no file is written
	when neither is set. The file rotates, sized by
	$REWARDS_FARMER_LOG_MAX_BYTES and $REWARDS_FARMER_LOG_BACKUPS.
	"""
	global _configured

	if _configured:
		return

	root = logging.getLogger()
	root.setLevel(_resolve_level(level))

	formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

	# Card descriptions are scraped from the page and are not ASCII outside the
	# en-US market, which the Windows console encoding cannot represent. Replace
	# those characters instead of letting the write raise.
	if hasattr(sys.stdout, "reconfigure"):
		sys.stdout.reconfigure(errors="replace")

	# stdout rather than the StreamHandler default of stderr, because this
	# replaces print and anyone already redirecting stdout to a file should
	# keep getting the same output there.
	console = logging.StreamHandler(sys.stdout)
	console.setFormatter(formatter)
	root.addHandler(console)

	if log_file is None:
		log_file = os.environ.get(FILE_ENV_VAR)

	if log_file:
		handler = _file_handler(log_file)

		if handler is not None:
			handler.setFormatter(formatter)
			root.addHandler(handler)

	for name in NOISY_LIBRARIES:
		logging.getLogger(name).setLevel(logging.WARNING)

	_configured = True
