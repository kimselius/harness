# Image for repeated use: the harness and its tooling are baked in, so startup is
# instant and the agent's filesystem dies with the container.
#
#   docker build -t harness .
#   docker run -it --rm --env-file .env \
#     -v "$PWD/workspace":/ws -v "$PWD/logs":/logs \
#     -e HARNESS_WORKSPACE=/ws -e HARNESS_LOG_DIR=/logs harness
#
# The agent runs as root with no sandbox beyond path confinement. That is the
# point: throw the container away, not a VM.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TERM=xterm-256color

# python3 runs the harness; the rest is what an agent reaches for constantly.
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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /harness

# Dependencies first, so source edits don't invalidate the pip layer.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY *.py ./
COPY tests/ ./tests/
# Matches PROMPT.md.example always, and your own PROMPT.md when it exists.
COPY PROMPT.md* ./

RUN mkdir -p workspace logs

# Args land on main.py: bare `docker run -it harness` opens the REPL, and
# `docker run --rm harness -p "do a thing"` runs one shot.
ENTRYPOINT ["python3", "main.py"]
