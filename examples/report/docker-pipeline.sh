#!/bin/sh
# The same seven steps, but each one is its own container.
#
# Every step is a fresh `docker run` that exits when its agent finishes. They
# share one named volume, which is the whole handoff: step 2 reads what step 1
# wrote to /work. `docker run` without -d blocks and returns the agent's exit
# code, so this script can fail fast the same way run.sh does.
#
# Use this shape when you want each step isolated (its own filesystem, its own
# memory, nothing inherited), or when the steps run on different hosts against a
# shared volume. Use run.sh inside a single container when you just want the
# pipeline to be cheap — see README.md.
#
#   docker build -t harness ../..
#   ./docker-pipeline.sh
#
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
IMAGE=${IMAGE:-harness}
VOLUME=${VOLUME:-report-work}
MAX_SECONDS=${MAX_SECONDS:-900}
ONLY=${1:-all}

# Credentials: an --env-file if there is one, otherwise pass the variables
# through from this shell. `--env NAME` with no value does not put the secret on
# the command line, so it stays out of `docker inspect` and the process table.
ENV_FILE=${ENV_FILE:-$HERE/../../.env}

say() { printf '%s\n' "$*" >&2; }

docker volume create "$VOLUME" >/dev/null

# The volume starts out owned by root; the image runs as uid 1001 and has to be
# able to write to it. One throwaway root container fixes that, idempotently.
docker run --rm --user 0:0 --entrypoint chown \
    -v "$VOLUME:/work" "$IMAGE" -R 1001:1001 /work

# step <label> <prompt-file> <instruction...>
step() {
    label=$1
    prompt=$2
    shift 2
    instruction=$*
    say ""
    say "── $label (container)"
    # Positional parameters rather than a string: the env flags differ between
    # the two cases, and a path with a space in it must survive.
    if [ -f "$ENV_FILE" ]; then
        set -- --env-file "$ENV_FILE"
    else
        set -- --env OPENAI_API_KEY --env OPENAI_BASE_URL --env HARNESS_MODEL
    fi
    docker run --rm "$@" \
        -v "$VOLUME:/work" \
        -v "$HERE/prompts:/prompts:ro" \
        "$IMAGE" \
        --workspace /work \
        --log-dir /work/.harness-logs \
        --prompt-file "/prompts/$prompt" \
        --label "$label" \
        --result-file "/work/.pipeline/$label.json" \
        --max-seconds "$MAX_SECONDS" \
        --quiet \
        -p "$instruction" </dev/null || {
        code=$?
        say "!! step '$label' failed with exit $code — nothing after it ran"
        say "   its result: docker run --rm -v $VOLUME:/work $IMAGE \\"
        say "       --workspace /work -p 'print .pipeline/$label.json'"
        exit "$code"
    }
}

# Reading a file out of the volume needs a container too, since the volume is not
# on this filesystem.
volume_read() {
    docker run --rm -v "$VOLUME:/work" --user 0:0 --entrypoint cat "$IMAGE" "/work/$1"
}

want() { [ "$ONLY" = all ] || [ "$ONLY" = "$1" ]; }

if ! docker run --rm -v "$VOLUME:/work" --user 0:0 --entrypoint sh "$IMAGE" \
        -c '[ -n "$(ls -A /work/notes 2>/dev/null)" ]'; then
    say "The volume has no notes yet. Copy some in:"
    say "  tar -C ./sample-notes -cf - . | docker run --rm -i -v $VOLUME:/work \\"
    say "      --user 0:0 --entrypoint sh $IMAGE \\"
    say "      -c 'mkdir -p /work/notes && tar -C /work/notes -xf - && chown -R 1001:1001 /work'"
    exit 1
fi

if want outline; then
    step outline 01-outline.md \
        "Read every file in notes/ and plan the report. Write outline.txt and plan.md."
fi

# Copy the outline out to a host file. The fan-out loop must run in *this* shell:
# `… | while read` would put it in a subshell, where a failing step's `exit` only
# ends the subshell and the pipeline would carry on past a broken step.
OUTLINE_FILE=$(mktemp)
trap 'rm -f "$OUTLINE_FILE"' EXIT INT TERM
volume_read outline.txt > "$OUTLINE_FILE" 2>/dev/null || true
if [ ! -s "$OUTLINE_FILE" ]; then
    say "!! outline.txt is empty — cannot fan out. Re-run: $0 outline"
    exit 1
fi
if ! grep -q "$(printf '\t')" "$OUTLINE_FILE"; then
    say "!! outline.txt has no tab-separated lines; it looks like this:"
    head -3 "$OUTLINE_FILE" >&2
    exit 1
fi

if want sections; then
    while IFS="$(printf '\t')" read -r id title; do
        [ -n "${id:-}" ] || continue
        step "write-$id" 02-section.md \
            "Write the section with id '$id', titled '$title', to sections/$id.md."
    done < "$OUTLINE_FILE"
fi

if want references; then
    while IFS="$(printf '\t')" read -r id title; do
        [ -n "${id:-}" ] || continue
        step "refs-$id" 03-references.md \
            "Add source markers and a Sources list to sections/$id.md (id '$id')."
    done < "$OUTLINE_FILE"
fi

if want assemble; then
    step assemble 04-assemble.md \
        "Assemble every file in sections/ into report.md, in the order outline.txt gives."
fi

if want review; then
    step review 05-review.md \
        "Check report.md against instructions.md and the plan. Write corrections.md. Do not edit the report."
fi

if want fix; then
    step fix 06-fix.md \
        "Apply every ACTION: FIX entry in corrections.md to report.md, then record what you applied."
fi

if want summary; then
    step summary 07-summary.md \
        "Write executive-summary.md from report.md and corrections.md."
fi

say ""
if want summary; then
    say "done. the report lives in the '$VOLUME' volume:"
    say "  docker run --rm -v $VOLUME:/work --user 0:0 --entrypoint cat $IMAGE \\"
    say "      /work/executive-summary.md"
else
    say "step '$ONLY' done. the volume '$VOLUME' holds what it wrote."
fi
