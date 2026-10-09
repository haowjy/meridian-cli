"""Run the prepared-environment fast developer gate with one wall-clock budget."""

from __future__ import annotations

import argparse
import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

DEFAULT_BUDGET_SECONDS = 60.0
EXIT_BUDGET_EXHAUSTED = 124


def _stop_command(process: subprocess.Popen[bytes]) -> None:
    """Kill the owned POSIX group, including descendants of an exited leader."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def run_commands(
    commands: Sequence[Sequence[str]],
    *,
    cwd: Path,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
) -> int:
    """Run independent checks concurrently; stop every owned group on exit."""
    if not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise ValueError("budget_seconds must be finite and positive")
    started = time.monotonic()
    deadline = started + budget_seconds
    active: set[subprocess.Popen[bytes]] = set()
    try:
        for command in commands:
            if time.monotonic() >= deadline:
                print(f"preflight: fast budget exhausted ({budget_seconds:g}s)", file=sys.stderr)
                return EXIT_BUDGET_EXHAUSTED
            print(f"preflight: {' '.join(command)}", file=sys.stderr)
            active.add(subprocess.Popen(list(command), cwd=cwd, start_new_session=True))

        while active:
            if time.monotonic() >= deadline:
                print(f"preflight: fast budget exhausted ({budget_seconds:g}s)", file=sys.stderr)
                return EXIT_BUDGET_EXHAUSTED
            for process in tuple(active):
                status = process.poll()
                if status is None:
                    continue
                # A completed leader does not prove that its descendants exited.
                _stop_command(process)
                active.remove(process)
                if status != 0:
                    return status
            if active:
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        print(f"preflight: fast gate passed in {time.monotonic() - started:.2f}s", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        print(f"preflight: cannot run check: {error}", file=sys.stderr)
        return 127
    finally:
        # Also runs after a partial launch failure, failed check or interruption.
        # No per-command grace can extend the shared deadline.
        for process in active:
            _stop_command(process)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if not math.isfinite(args.budget) or args.budget <= 0:
        parser.error("--budget must be finite and positive")
    python = sys.executable
    commands = (
        (python, "-m", "ruff", "check", "."),
        (python, "-m", "pyright"),
        (python, "-m", "meridian.dev.pytests"),
    )
    # A gate must not silently become a last-failed/filtered developer rerun.
    os.environ.pop("PYTESTS_LAST_FAILED", None)
    os.environ.pop("PYTEST_ADDOPTS", None)
    print(f"preflight: fast gate (default testpaths, budget={args.budget:g}s)", file=sys.stderr)
    return run_commands(commands, cwd=args.repo, budget_seconds=args.budget)


if __name__ == "__main__":
    raise SystemExit(main())
