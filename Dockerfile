# Runs the bot without installing Edge, a driver or Python on the host, and by
# default runs it once a day rather than once.
#
# The image carries only what a run actually reaches: selenium and numpy.
# pygetwindow, keyboard, matplotlib and pygame are used solely by the
# recording and visualisation scripts, which are developer tools rather than
# part of a run, and two of them are Windows-only. The scheduler is stdlib.
#
# QUERY_SOURCE defaults to trends here so a container needs no Ollama account
# and no model download. Set it to llm and point OLLAMA_HOST at a reachable
# host to use a model instead.

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# Edge, from Microsoft's own repository. tzdata as well: the schedule is a local
# time, and without the zone database every REWARDS_TZ falls back to UTC, which
# on a European home server is a run at the wrong hour.
#
# Xvfb, x11vnc and a minimal window manager are for the one thing this image
# cannot do headless: signing in. The profile has to be signed in once by hand,
# a Proxmox node has no screen, and a profile signed in on Windows is unreadable
# here because its cookies are wrapped with a key only Windows can unwrap. They
# are used by `docker compose run signin` and by nothing else.
RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		ca-certificates curl gnupg unzip fonts-liberation tzdata \
		xvfb x11vnc fluxbox \
	&& curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
		| gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
	&& echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/edge stable main" \
		> /etc/apt/sources.list.d/microsoft-edge.list \
	&& apt-get update \
	&& apt-get install -y --no-install-recommends microsoft-edge-stable \
	&& rm -rf /var/lib/apt/lists/*

# The driver has to match the browser build, so it is pinned to whatever Edge
# the layer above installed rather than to "latest", which drifts apart from it
# between releases.
RUN EDGE_VERSION="$(microsoft-edge --version | awk '{print $3}')" \
	&& curl -fsSL -o /tmp/edgedriver.zip \
		"https://msedgedriver.microsoft.com/${EDGE_VERSION}/edgedriver_linux64.zip" \
	&& unzip -j /tmp/edgedriver.zip msedgedriver -d /usr/local/bin \
	&& chmod +x /usr/local/bin/msedgedriver \
	&& rm /tmp/edgedriver.zip \
	&& msedgedriver --version

WORKDIR /app

RUN pip install --no-cache-dir "selenium>=4.46.0,<5.0.0" "numpy"

COPY src/ ./src/
COPY docker/entrypoint.sh docker/signin.sh /usr/local/bin/
COPY nouns.txt ./

# Not root. A browser rendering pages from the open internet is the last process
# on a home server that should be running as uid 0, and it does not need to be:
# the profile, the state and the code are all this user's.
#
# The home directory is world writable on purpose. The compose file lets the
# uid be overridden so the bind mounted profile matches its owner on the host,
# and Chromium wants a writable HOME whichever uid it ends up as.
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/signin.sh \
	&& useradd --create-home --uid 1000 --shell /usr/sbin/nologin farmer \
	&& mkdir -p /app/data-dir /app/state \
	&& chown -R farmer:farmer /app /home/farmer \
	&& chmod 0777 /home/farmer

ENV REWARDS_HEADLESS=1 \
	QUERY_SOURCE=trends \
	PYTHONUNBUFFERED=1 \
	HOME=/home/farmer \
	REWARDS_DATA_DIR=/app/data-dir \
	REWARDS_STATE_DIR=/app/state \
	REWARDS_FARMER_LOG_FILE=/app/state/rewards-farmer.log

# Sign-in lives in the first, the record of what ran lives in the second, and
# both have to outlive the container.
VOLUME ["/app/data-dir", "/app/state"]

USER farmer

# Unhealthy once no run has succeeded for two days, which is the difference
# between a container that is idle between runs and one that is wedged. The
# start period covers a fresh install, whose first run has not happened yet.
HEALTHCHECK --interval=15m --timeout=30s --start-period=2m --retries=3 \
	CMD ["python", "src/scheduler.py", "--health"]

ENTRYPOINT ["entrypoint.sh"]
CMD ["daily"]
