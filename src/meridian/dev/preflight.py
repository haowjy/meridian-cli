"""Run the prepared-environment fast developer gate with a wall-clock target."""

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


def _stop_command(process: subprocess.Popen[bytes]) -> int:
    """Kill the owned group before reaping its leader and releasing its id."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    return process.wait()


def _warn_if_over_budget(elapsed: float, budget_seconds: float) -> None:
    if elapsed <= budget_seconds:
        return
    message = f"fast gate took {elapsed:.1f}s (target {budget_seconds:g}s)"
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=Fast gate over budget::{message}", file=sys.stderr)
    else:
        print(f"preflight: warning: {message}", file=sys.stderr)


def run_commands(
    commands: Sequence[Sequence[str]],
    *,
    cwd: Path,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
) -> int:
    """Run checks concurrently; fail fast and warn when they miss the target."""
    if not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise ValueError("budget_seconds must be finite and positive")
    started = time.monotonic()
    active: set[subprocess.Popen[bytes]] = set()
    try:
        for command in commands:
            print(f"preflight: {' '.join(command)}", file=sys.stderr)
            active.add(subprocess.Popen(list(command), cwd=cwd, start_new_session=True))

        while active:
            for process in tuple(active):
                # Peek without reaping: the leader reserves the numeric group
                # id until cleanup, even if all other members already exited.
                exited = os.waitid(
                    os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
                if exited is None or exited.si_pid == 0:
                    continue
                # A completed leader does not prove that its descendants exited.
                status = _stop_command(process)
                active.remove(process)
                if status != 0:
                    return status
            if active:
                time.sleep(0.05)
        print(f"preflight: fast gate passed in {time.monotonic() - started:.2f}s", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        print(f"preflight: cannot run check: {error}", file=sys.stderr)
        return 127
    finally:
        # Also runs after a partial launch failure, failed check or interruption.
        for process in active:
            _stop_command(process)
        _warn_if_over_budget(time.monotonic() - started, budget_seconds)


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
