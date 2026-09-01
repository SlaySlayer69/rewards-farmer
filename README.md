# User0332/rewards-farmer

Automation for MS Rewards based on [https://youtu.be/4qdPcMNaioA](https://youtu.be/4qdPcMNaioA).

Run it by hand, or leave it to run itself once a day on a machine that is already on — see [Running it every day on a home server](#running-it-every-day-on-a-home-server) for Proxmox, Docker and systemd.

# Running Instructions

IMPORTANT: Use at your own risk. Microsoft may take action against your account for using automated scripts to gain rewards points. The YouTube video contains more details about the techniques implemented to avoid detection of this script.

Clone the repository.

```sh
git clone https://github.com/User0332/rewards-farmer
```

A sample `nouns.txt` file is included in the project root and can be modified by the user to contain seed words for the LLM to complete 20 searches. The wordlist should be separated by newline.

```sh
cd rewards-farmer
# Edit the included nouns.txt file to add or replace words as needed
```

# Where search queries come from

The bot needs short strings to type into Bing. Two backends produce them, set with `QUERY_SOURCE`:

| `QUERY_SOURCE` | Needs | Notes |
| --- | --- | --- |
| `llm` (default) | Ollama account + model | Current behaviour, unchanged |
| `trends` | nothing | Google Trends, Wikipedia and Bing autosuggest |

```sh
QUERY_SOURCE=trends python src/main.py          # bash
$env:QUERY_SOURCE="trends"; python src/main.py  # PowerShell
```

`trends` needs no account, no API key and no model download, so the Ollama setup below is optional if you use it. If every feed is unreachable it falls back to `nouns.txt` rather than failing the run.

You should also have an Ollama account created (for the LLM), the `ollama` tool installed, and you should have signed in to the Ollama CLI via the command line using `ollama signin`. This project will use a minimal amount of Ollama cloud usage using `gemma4:cloud`. If you wish to use a different model, please change the `model` parameter in the `get_ollama_response` function in `src/llm_utils.py`.

You must also provide an image for the script to upload to complete the visual search task. A helper script is included at `src/random_image_for_visual_search.py` that will download an image from Wikipedia named `visual_search.jpg` into the project root for you. You may also provide an image of your own, just ensure that the absolute path of the image is placed in the `VISUAL_SEARCH_IMAGE_PATH` constant at the top of `rewards_tasks.py`.

Activate the virtual environment & install dependencies (you may have to use `python -m poetry` instead of `poetry`).
You must have Python 3.12+ and Poetry installed.

If `iex (poetry env activate)` fails with *"Cannot bind argument to parameter 'Command' because it is null"*, `poetry install` did not create an environment. Run `python --version` first: an older Python leaves poetry with nothing to activate, and the message explaining that goes to stderr rather than into `iex`.

Windows (PowerShell)
```sh
poetry install
iex (poetry env activate)
```

*nix (Bash)
```sh
poetry install
eval $(poetry env activate)
```

You must also have a [webdriver for Microsoft Edge](https://learn.microsoft.com/en-us/microsoft-edge/webdriver/?tabs=c-sharp) installed. If you already have the Edge Browser installed, you probably have this component as well.

The profile directory in `src/constants.py` is set to `Default`. If this signs you in to a global profile that you do not want to use for automation, then you can create a new profile from within the webdriver instance manually and then change the `PROFILE_NAME` constant to `Profile 1` (or the equivalent number).

Run main.py (`python src/main.py`, from anywhere: the profile, the wordlist and the visual search image are all found relative to the repository), wait for the page to launch, and then CTRL-C to quit the application immediately. Sign in to the created profile with your Microsoft account on both Bing and `rewards.bing.com`.

EU Users: you may have to accept a consent banner once on `rewards.bing.com` and on the Bing search page, `bing.com`. Once you consent, your choice will be saved for future runs using the same profile, so you will not need to interact with the banner during automated runs.

Close all webdriver browser instances. Run `main.py` again; the automation should start working.

# Running more than one account

Rewards is per Microsoft account and the browser profile holds the sign-in, so an account here is a profile directory. `REWARDS_ACCOUNTS` takes a comma separated list, and each name gets its own directory under `data-dir`:

```sh
REWARDS_ACCOUNTS=personal,spare python src/main.py
```

Each is signed in once by hand, the same way as the single profile, using its own directory:

```
msedge --user-data-dir="<repo>\data-dir\personal" --profile-directory=Default https://rewards.bing.com
```

They run one after another, and an account that fails is reported and skipped rather than ending the run, whether it fails to start or dies partway through. Leave `REWARDS_ACCOUNTS` unset and everything behaves exactly as before, using the single profile in `data-dir`.

# Running it every day on a home server

Rewards resets daily, so the natural place for this is a machine that is already on. On a Proxmox node that means a container: Docker inside an LXC, or the bot installed directly with a systemd timer.

The daily run is not a cron line, because a home server is not up all the time and a skipped day is a day's points gone. It:

* runs once a day at a time you choose, offset by a random amount so it is not the same second every day,
* runs a **missed day as soon as the machine comes back**, rather than waiting for tomorrow,
* runs **once** per day and no more, whatever restarts in between,
* **kills a run that wedges**, so a browser that stops answering costs one day rather than every day after it,
* **retries a failed run** twice, thirty minutes apart, and then leaves the day alone,
* records what each run did, so you can ask.

## Proxmox, from nothing

On the Proxmox host, as root. This only creates the container; it installs nothing into it.

```sh
./deploy/proxmox/create-lxc.sh                       # CTID 150, 4GB, 2 cores
CTID=151 MEMORY=8192 ./deploy/proxmox/create-lxc.sh  # or pick your own
DRY_RUN=1 ./deploy/proxmox/create-lxc.sh             # print the pct command, run nothing
```

It makes an **unprivileged** container with `nesting=1` and `keyctl=1`, which is what Docker needs to run inside one. Give it at least 2GB of RAM; Edge is the hungry part, and below that a run fails in ways that look like Bing's fault — half-rendered pages, a browser that stops answering.

Then inside it:

```sh
pct enter 150
apt-get update && apt-get install -y git
git clone https://github.com/User0332/rewards-farmer /opt/rewards-farmer
/opt/rewards-farmer/deploy/proxmox/install.sh
```

That installs Docker, builds the image and writes a `.env` you can edit. Add `--native` instead if you would rather not enable nesting: it installs Edge, the matching driver and a virtualenv, and schedules the run with a systemd timer.

## Signing in, on a machine with no screen

The sign-in lives in the browser profile, and it has to get there once by hand. **A profile signed in on Windows or macOS will not work here**: those wrap the cookie encryption key with something only that machine can unwrap (DPAPI, the login Keychain), so the Linux container carries the file in and then cannot read a single cookie in it. It looks healthy and behaves as though it were logged out. On Linux, with no keyring running, Chromium falls back to a fixed key, which is why a profile signed in *on the server* works *on the server*.

So sign in there, over VNC:

```sh
cd /opt/rewards-farmer
docker compose run --rm --service-ports signin
```

The port is published on the container's loopback only, so tunnel it from your desktop and point any VNC client at `127.0.0.1:5900`:

```sh
ssh -N -L 5900:127.0.0.1:5900 root@<container ip>
```

Sign in on `rewards.bing.com` **and** on `bing.com`, accept the consent banner if your market shows one, then close the browser window. Set `REWARDS_VNC_PASSWORD` in `.env` if you want the VNC server to ask for one as well.

For more than one account, sign each in against its own directory:

```sh
REWARDS_SIGNIN_ACCOUNT=personal docker compose run --rm --service-ports signin
```

## Starting the daily run

```sh
docker compose up -d
docker compose logs -f
```

That is the whole thing. It comes back after a host reboot, and it is idle between runs rather than busy.

| Command | What it does |
| --- | --- |
| `docker compose run --rm rewards-farmer status` | what the last run did, and when the next one is |
| `docker compose run --rm rewards-farmer once` | a run right now, whatever the schedule says |
| `docker compose run --rm rewards-farmer next` | when the next run is due |
| `docker compose ps` | `healthy` until two days pass with no successful run |

Settings live in `.env`; copy `.env.example` and edit it.

| Variable | Default | Effect |
| --- | --- | --- |
| `REWARDS_SCHEDULE` | `09:00` | when the daily run starts, local time |
| `REWARDS_JITTER_MINUTES` | `90` | random offset added to it, fixed per day |
| `REWARDS_TZ` | `UTC` | the zone that time is written in |
| `REWARDS_CATCH_UP` | `1` | run a missed day at startup instead of waiting |
| `REWARDS_RETRY_MINUTES` | `30` | wait before retrying a failed run |
| `REWARDS_MAX_RETRIES` | `2` | retries per day, after which the day is left alone |
| `REWARDS_RUN_TIMEOUT_MINUTES` | `120` | a run that outlasts this is killed, browser and all |
| `REWARDS_UID` / `REWARDS_GID` | `1000` | who owns `data-dir` and `state` on the host |

## Without Docker: a systemd timer

`deploy/proxmox/install.sh --native` does this for you. By hand it is:

```sh
sudo cp deploy/systemd/rewards-farmer.* /etc/systemd/system/
sudo cp deploy/systemd/rewards-farmer.env.example /etc/rewards-farmer.env
sudo systemctl daemon-reload
sudo systemctl enable --now rewards-farmer.timer
```

`Persistent=true` in the timer is the catch-up, and `RandomizedDelaySec=90m` is the jitter, so systemd does what the scheduler does inside the container.

```sh
systemctl list-timers rewards-farmer.timer   # when it next runs
journalctl -u rewards-farmer.service -f      # what it did
sudo systemctl start rewards-farmer.service  # a run right now
sudo systemctl edit rewards-farmer.timer     # change the time of day
```

The unit runs as its own user with `/opt` read-only to it; the profile, the state and the log are in `/var/lib/rewards-farmer`.

## Where things are

Nothing is resolved against the working directory any more, so it does not matter where a timer or a service starts the bot from. Every path has a default in the repository and an environment variable that moves it:

| Variable | Default | Holds |
| --- | --- | --- |
| `REWARDS_DATA_DIR` | `<repo>/data-dir` | browser profiles, one directory per account |
| `REWARDS_STATE_DIR` | `<repo>/state` | `state.json`, the run lock, the rotating log |
| `REWARDS_NOUNS_FILE` | `<repo>/nouns.txt` | seed wordlist |
| `REWARDS_VISUAL_SEARCH_IMAGE` | `<repo>/visual_search.jpg` | the image the visual search task uploads |

`state.json` is small and readable — last run, exit code, how many failures in a row, the last fortnight of runs — and `status` above just prints it.

## When it stops earning

`docker compose ps` reports the container unhealthy once two days pass with no successful run, which is the difference between idle between runs and wedged. Then:

```sh
docker compose run --rm rewards-farmer status
docker compose logs --tail 200
```

The usual cause is the sign-in having expired: `[SKIP]` on every task, or a run that finishes in seconds. Sign in again with the `signin` service above.

If a run is killed while the browser is open — a power cut, `docker kill` — Chromium leaves a lock naming a process that no longer exists, and every later run would exit during startup. That is cleared automatically on the next run, which is why the compose file pins the container's hostname: the lock names the machine that wrote it, and a container whose name changes every start cannot recognise its own.

# Docker, by hand

The image also runs a single pass, without any of the scheduling:

```sh
docker compose build
docker compose run --rm rewards-farmer once
```

The container defaults to `QUERY_SOURCE=trends`, so it needs no Ollama account and no model. Set `QUERY_SOURCE=llm` and `OLLAMA_HOST` to a reachable address to use a model instead.

**Sign in first.** The profile in `data-dir` starts logged out. Use the `signin` service above, or do it on the host with a normal Edge window and let the volume carry it in — on a Linux host, for the reason given above:

```
msedge --user-data-dir="<repo>\data-dir" --profile-directory=Default https://rewards.bing.com
```

Close every window of that profile afterwards. Chromium allows one process per profile directory, so a window left open on the host stops the container from starting. A profile whose browser was *killed* keeps a `SingletonLock` naming the machine that wrote it; a run clears that itself when the process it names is gone, and leaves it alone when the name belongs to another machine, since from inside there is no telling that apart from a profile genuinely open elsewhere. `REWARDS_FORCE_PROFILE_UNLOCK=1` overrides that, for when the other machine is not coming back.

**This does not work from a Windows host.** Chromium encrypts cookie values with a key held by the operating system, and on Windows that key is wrapped with DPAPI and tied to the Windows account that wrote it. The Linux container has no DPAPI, so it cannot unwrap the key and every cookie in the profile is unreadable to it. The volume carries the file in and the browser then ignores its contents: a profile signed in on the host reported 73 cookies on disk, of which Edge in the container could read 19 — the ones it had just set itself — while `.MSA.Auth` and `ANON`, the cookies the sign-in actually rests on, came back absent. The container starts, looks healthy and behaves as though it were logged out.

Sign-in has to happen wherever the container will read it. From a Windows host that means signing in *inside* the container, over VNC:

```sh
docker compose run --rm --service-ports signin
```

or not using the container at all, and running the bot directly:

```sh
python src/main.py
```

**A Linux host does work.** With no keyring running Chromium falls back to a fixed key, which is the case both on a plain Linux host and inside the image, so the volume carries a working sign-in straight in. Measured: a profile signed in on the host opened in the container already on `rewards.bing.com/dashboard` and earned from it.

macOS is expected to fail the way Windows does, since it wraps the key with the login Keychain and the container cannot reach that either, but that case was not tested.

**Provide the visual search image on the host too.** `visual_search.jpg` is not in the repository and is not built into the image, so create it once in the project root and the compose file mounts it in:

```sh
python src/random_image_for_visual_search.py
```

Without it every other task still runs; only the visual search one fails.

Multiple accounts work the same way in the container:

```sh
REWARDS_ACCOUNTS=personal,spare docker compose run --rm rewards-farmer once
```

`REWARDS_HEADLESS=1` is set in the image. It also works on the host if you want a run with no visible window; the pointer code needs an explicit window size in that mode, which `main.py` sets.

# Logging

The script logs to the console. Two optional environment variables change that:

| Variable | Default | Effect |
| --- | --- | --- |
| `REWARDS_FARMER_LOG_LEVEL` | `INFO` | Set to `DEBUG` to also attach the full stack trace to every `[FAIL]` line. |
| `REWARDS_FARMER_LOG_FILE` | unset | Path to also write the log to, useful for unattended runs. |
| `REWARDS_FARMER_LOG_MAX_BYTES` | `5242880` | The log file rotates at this size. |
| `REWARDS_FARMER_LOG_BACKUPS` | `4` | How many rotated files to keep. |

The Docker and systemd installs set the file for you, to `rewards-farmer.log` in the state directory. It rotates, because a run a day for a year does not fit in one file.

Windows (PowerShell)
```sh
$env:REWARDS_FARMER_LOG_LEVEL="DEBUG"; $env:REWARDS_FARMER_LOG_FILE="run.log"; python src/main.py
```

*nix (Bash)
```sh
REWARDS_FARMER_LOG_LEVEL=DEBUG REWARDS_FARMER_LOG_FILE=run.log python src/main.py
```

If you are opening an issue about a crash, running with `REWARDS_FARMER_LOG_LEVEL=DEBUG` and attaching the log is the most useful thing you can include.

Please open up a GitHub issue if you run into any difficulties.