# Image for repeated use: the harness and its tooling are baked in, so startup is
# instant and the agent's filesystem dies with the container.
#
#   docker build -t harness .
#   docker run -it --rm --env-file .env \
#     -v "$PWD/workspace":/ws -v "$PWD/logs":/logs \
#     -e HARNESS_WORKSPACE=/ws -e HARNESS_LOG_DIR=/logs harness
#
# Runs as uid 1001, not root. On Linux the mounted workspace and log directories
# must be writable by that uid: either `chown -R 1001:1001 workspace logs` on the
# host, or override with `--user "$(id -u):$(id -g)"`.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TERM=xterm-256color \
    HOME=/home/harness

# python3 runs the harness; the rest is what an agent reaches for constantly.
# tini is the exception — it is not for the agent, it is PID 1. See ENTRYPOINT.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ca-certificates \
        curl \
        git \
        jq \
        ripgrep \
        less \
        procps \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Dedicated unprivileged account. 1001 is free on Ubuntu 24.04, whose own
# "ubuntu" user already holds 1000.
RUN groupadd --gid 1001 harness \
    && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash harness

WORKDIR /harness

# Dependencies first (as root, into system site-packages), so source edits do not
# invalidate the pip layer.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY *.py ./
COPY tests/ ./tests/
# Matches PROMPT.md.example always, and your own PROMPT.md when it exists.
COPY PROMPT.md* ./

# Only these two need to be writable. The source stays owned by root, so the agent
# cannot modify the harness that is running it, even inside the container.
RUN mkdir -p workspace logs && chown -R 1001:1001 workspace logs

USER 1001:1001

# tini is PID 1, the harness is its child. Two measured reasons, both specific to
# an agent that spawns processes:
#
#  - PID 1 discards any signal it has no handler for. The harness installs a
#    SIGTERM handler, but not until the interpreter and the OpenAI SDK have
#    loaded, so a `docker stop` inside that window was silently dropped: the
#    container sat out the full ten second grace period and was then SIGKILLed,
#    losing the session_end record. With tini there is no window.
#  - A backgrounded command that outlives the shell that started it gets
#    reparented to PID 1 on exit. The harness never calls wait() on processes it
#    did not start, so with the harness as PID 1 those became permanent
#    `[sleep] <defunct>` zombies, one per backgrounded job. tini reaps them.
#
# Args still land on main.py: bare `docker run -it <image>` opens the REPL, and
# `docker run --rm <image> -p "do a thing"` runs one shot.
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "main.py"]
