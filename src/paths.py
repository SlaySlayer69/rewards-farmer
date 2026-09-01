"""Where the files a run needs actually live.

Every path in this project used to be resolved against the working directory:
`./data-dir`, `nouns.txt`, `visual_search.jpg`. That holds while the bot is
started by hand from the repository root, and breaks the moment anything else
starts it. Neither cron, nor a systemd timer, nor a container entrypoint
inherits the directory a human happened to be standing in, so a scheduled run
found no profile, created an empty one next to wherever it was launched from,
signed in to nobody and still reported success.

Paths are anchored to the repository instead, and each one can be pointed
somewhere else with an environment variable. That is what an unattended install
wants: the checkout wherever it was cloned, the profiles and the run state on a
volume that survives a rebuild.

	REWARDS_DATA_DIR              browser profiles, one directory per account
	REWARDS_STATE_DIR             run state, the lock file and the log
	REWARDS_NOUNS_FILE            seed wordlist
	REWARDS_VISUAL_SEARCH_IMAGE   image the visual search task uploads
"""

import os
from pathlib import Path

# src/paths.py -> src -> repository root.
REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR_ENV = "REWARDS_DATA_DIR"
STATE_DIR_ENV = "REWARDS_STATE_DIR"
NOUNS_ENV = "REWARDS_NOUNS_FILE"
VISUAL_SEARCH_ENV = "REWARDS_VISUAL_SEARCH_IMAGE"


def _configured(variable: str, default: Path) -> Path:
	"""The path in `variable`, or `default`.

	A relative override is taken against the repository rather than the working
	directory, since the working directory is the thing this module exists to
	stop depending on.
	"""
	value = os.environ.get(variable, "").strip()

	if not value:
		return default

	path = Path(value).expanduser()

	return path if path.is_absolute() else (REPO_ROOT / path)


def data_dir() -> Path:
	"""Root of the browser profiles. One subdirectory per named account."""
	return _configured(DATA_DIR_ENV, REPO_ROOT / "data-dir")


def state_dir() -> Path:
	"""Run state, lock file and log file.

	Deliberately not under `data-dir`: account names become directories there,
	so anything else living beside them is one unlucky account name away from a
	collision.
	"""
	return _configured(STATE_DIR_ENV, REPO_ROOT / "state")


def nouns_file() -> Path:
	return _configured(NOUNS_ENV, REPO_ROOT / "nouns.txt")


def visual_search_image() -> Path:
	return _configured(VISUAL_SEARCH_ENV, REPO_ROOT / "visual_search.jpg")


def ensure_dir(path: Path) -> Path:
	"""Create `path` if it is missing. Returns it either way.

	Failure is not raised. A missing state directory costs the run its
	bookkeeping; it must not cost the run itself, which is the only part that
	earns anything.
	"""
	try:
		path.mkdir(parents=True, exist_ok=True)
	except OSError:
		pass

	return path
