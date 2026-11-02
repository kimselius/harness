#!/bin/sh
# Seven agents, one after another, each finishing before the next starts.
#
# The handoff is the workspace, not a pipe: every step reads the files the
# previous step wrote. That is what makes a step re-runnable on its own, and why
# each step can start with an empty context.
#
# Runs unchanged on a host or inside a container (see README.md in this
# directory). POSIX sh, no jq, no bash.
#
#   ./run.sh                      # whole pipeline
#   ./run.sh outline              # just the first step
#   WORKSPACE=/tmp/w ./run.sh     # somewhere else
#
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
HARNESS=${HARNESS:-$(cd "$HERE/../.." && pwd)}
WORKSPACE=${WORKSPACE:-$HARNESS/workspace/report}
RESULTS=${RESULTS:-$WORKSPACE/.pipeline}

# A venv if there is one, otherwise whatever python3 is on PATH (the container
# case, where dependencies are installed system-wide).
if [ -x "$HARNESS/.venv/bin/python" ]; then
    PYTHON=${PYTHON:-$HARNESS/.venv/bin/python}
else
    PYTHON=${PYTHON:-python3}
fi

# Per-step wall-clock ceiling. A step that wedges fails the pipeline instead of
# hanging a cron job until morning.
MAX_SECONDS=${MAX_SECONDS:-900}
ONLY=${1:-all}

# Session transcripts. The default matches the harness's own, so `main.py
# --sessions` finds them on a host. Inside a container, point LOG_DIR at the
# mounted volume or the transcripts die with the container.
LOG_DIR=${LOG_DIR:-$HARNESS/logs}

mkdir -p "$WORKSPACE" "$RESULTS"

say() { printf '%s\n' "$*" >&2; }

# step <label> <prompt-file> <instruction...>
step() {
    label=$1
    prompt=$2
    shift 2
    say ""
    say "── $label"
    # stdin from /dev/null: these calls run inside a `while read` loop below, and
    # a child that inherits the loop's stdin would eat the rest of the outline.
    "$PYTHON" "$HARNESS/main.py" \
        --workspace "$WORKSPACE" \
        --log-dir "$LOG_DIR" \
        --prompt-file "$HERE/prompts/$prompt" \
        --label "$label" \
        --result-file "$RESULTS/$label.json" \
        --max-seconds "$MAX_SECONDS" \
        --quiet \
        -p "$*" </dev/null || {
        code=$?
        say "!! step '$label' failed with exit $code"
        case $code in
            1) say "   the model or the endpoint failed; see $RESULTS/$label.json" ;;
            3) say "   it ran out of tool rounds — raise --max-steps or split the step" ;;
            4) say "   it ran past MAX_SECONDS=$MAX_SECONDS" ;;
            130|143) say "   interrupted" ;;
        esac
        say "   nothing after this step ran. Fix it, then: $0 $label"
        exit "$code"
    }
}

want() { [ "$ONLY" = all ] || [ "$ONLY" = "$1" ]; }

# -- 0. the input ----------------------------------------------------------
if [ ! -d "$WORKSPACE/notes" ]; then
    say "No notes to work from."
    say "Put your notes in $WORKSPACE/notes/, or start from the samples:"
    say "  mkdir -p $WORKSPACE/notes && cp $HERE/sample-notes/* $WORKSPACE/notes/"
    exit 1
fi

# -- 1. decide what the report contains ------------------------------------
if want outline; then
    step outline 01-outline.md \
        "Read every file in notes/ and plan the report. Write outline.txt and plan.md."

    # Never trust a model to have produced parseable output. The loop below
    # depends on this file, so check it here where the error is still local.
    if [ ! -s "$WORKSPACE/outline.txt" ]; then
        say "!! outline.txt is missing or empty — cannot fan out"
        exit 1
    fi
    if ! grep -q "$(printf '\t')" "$WORKSPACE/outline.txt"; then
        say "!! outline.txt has no tab-separated lines; it looks like this:"
        head -3 "$WORKSPACE/outline.txt" >&2
        say "   re-run: $0 outline"
        exit 1
    fi
    say ""
    say "planned $(grep -c . "$WORKSPACE/outline.txt") sections"
fi

# From here on every step needs the outline.
[ -s "$WORKSPACE/outline.txt" ] || { say "run '$0 outline' first"; exit 1; }

# -- 2. write each section, one agent per section ---------------------------
# One short context per section beats one agent holding the whole report.
if want sections; then
    while IFS="$(printf '\t')" read -r id title; do
        [ -n "${id:-}" ] || continue
        step "write-$id" 02-section.md \
            "Write the section with id '$id', titled '$title', to sections/$id.md."
    done < "$WORKSPACE/outline.txt"
fi

# -- 3. source each section -------------------------------------------------
if want references; then
    while IFS="$(printf '\t')" read -r id title; do
        [ -n "${id:-}" ] || continue
        [ -f "$WORKSPACE/sections/$id.md" ] || { say "skipping $id: not written"; continue; }
        step "refs-$id" 03-references.md \
            "Add source markers and a Sources list to sections/$id.md (id '$id')."
    done < "$WORKSPACE/outline.txt"
fi

# -- 4. assemble ------------------------------------------------------------
if want assemble; then
    step assemble 04-assemble.md \
        "Assemble every file in sections/ into report.md, in the order outline.txt gives."
fi

# -- 5. review against the brief -------------------------------------------
# Finding problems and fixing them used to be one step. It read the report, the
# plan, every section and the notes, then edited — 48 tool calls and 450k input
# tokens, and it hit the 30-round ceiling. Two steps, each with one job.
if want review; then
    step review 05-review.md \
        "Check report.md against instructions.md and the plan. Write corrections.md. Do not edit the report."
fi

# -- 6. apply the corrections ----------------------------------------------
if want fix; then
    step fix 06-fix.md \
        "Apply every ACTION: FIX entry in corrections.md to report.md, then record what you applied."
fi

# -- 7. executive summary ---------------------------------------------------
if want summary; then
    step summary 07-summary.md \
        "Write executive-summary.md from report.md and corrections.md."
fi

say ""
say "done. artifacts in $WORKSPACE:"
for file in outline.txt plan.md report.md corrections.md executive-summary.md; do
    # `|| true` matters under `set -e`: a missing file would otherwise make the
    # last command of the loop fail and take the whole script's exit code with it.
    [ -f "$WORKSPACE/$file" ] && say "  $file" || true
done
say "  step results in $RESULTS/"
