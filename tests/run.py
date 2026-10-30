#!/usr/bin/env python3
"""Run every suite. Usage: python tests/run.py [name ...]

Each suite exposes main_() (or main()) returning an exit code. They run in one
process, so common.reset() separates their results.
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import common  # noqa: E402

# Cheap and deterministic first, slow pty/subprocess suites last.
SUITES = [
    "markdown",
    "harness",
    "compaction",
    "cwd",
    "platform",
    "security",
    "reasoning",
    "sessions",
    "interrupt",
    "steer",
    "paste",
]


def run_one(name: str, quiet: bool) -> tuple[int, int, int, float]:
    """Returns (failed, passed, exit_code, seconds)."""
    module = importlib.import_module(f"test_{name}")
    entry = getattr(module, "main_", None) or module.main
    common.reset()
    started = time.monotonic()
    if quiet:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()) as sink:
            code = entry()
        if common.failures:  # only show the detail that matters
            print(
                "\n".join(
                    line for line in sink.getvalue().splitlines()
                    if line.startswith("  FAIL")
                )
            )
    else:
        code = entry()
    return len(common.failures), len(common.passes), code, time.monotonic() - started


def main() -> int:
    wanted = sys.argv[1:] or SUITES
    unknown = [name for name in wanted if name not in SUITES]
    if unknown:
        print(f"unknown suite(s): {', '.join(unknown)}")
        print(f"available: {', '.join(SUITES)}")
        return 2

    quiet = len(wanted) > 1
    total_pass = total_fail = 0
    worst = 0
    for name in wanted:
        failed, passed, code, seconds = run_one(name, quiet)
        total_pass += passed
        total_fail += failed
        worst = max(worst, code)
        if not failed and not passed:
            # A suite that asserted nothing is broken — most likely it kept its
            # own check() and common never saw the results.
            failed = 1
            total_fail += 1
            print(f"{name}: no checks recorded, is it using common.check?")
        status = "ok" if not failed and code == 0 else "FAIL"
        print(f"{name:<11} {passed:>3} checks  {seconds:5.1f}s  {status}")

    print()
    print(f"{total_pass} checks passed, {total_fail} failed")
    return 1 if (total_fail or worst) else 0


if __name__ == "__main__":
    sys.exit(main())
