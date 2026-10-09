"""Run the prepared-environment fast developer gate with one wall-clock budget."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

DEFAULT_BUDGET_SECONDS = 60.0
EXIT_BUDGET_EXHAUSTED = 124


def _terminate_process_tree(process: subprocess.Popen[bytes], *, reason: str) -> None:
    """Stop a command and every process it started in its own process group."""

    print(f"preflight: stopping command ({reason})", file=sys.stderr)
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:  # pragma: no cover - native Windows is not a supported target.
        process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        else:  # pragma: no cover - native Windows is not a supported target.
            process.kill()
        process.wait()


def _run_commands_parallel(
    commands: Sequence[Sequence[str]], *, cwd: Path, deadline: float
) -> int:
    """Run independent gate commands concurrently under one deadline."""

    processes: list[subprocess.Popen[bytes]] = []
    try:
        for command in commands:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for process in processes:
                    _terminate_process_tree(process, reason="fast budget exhausted")
                return EXIT_BUDGET_EXHAUSTED
            print(f"preflight: {' '.join(command)}", file=sys.stderr)
            processes.append(
                subprocess.Popen(
                    list(command),
                    cwd=cwd,
                    start_new_session=os.name == "posix",
                )
            )

        active = set(processes)
        while active:
            for process in tuple(active):
                status = process.poll()
                if status is None:
                    continue
                active.remove(process)
                if status != 0:
                    for other in active:
                        _terminate_process_tree(other, reason="another gate command failed")
                    return status
            if not active:
                return 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for process in active:
                    _terminate_process_tree(process, reason="fast budget exhausted")
                return EXIT_BUDGET_EXHAUSTED
            time.sleep(min(0.1, remaining))
        return 0
    except KeyboardInterrupt:
        for process in processes:
            _terminate_process_tree(process, reason="interrupted")
        return 130


def run_commands(
    commands: Sequence[Sequence[str]],
    *,
    cwd: Path,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
) -> int:
    """Run commands under one monotonic wall-clock budget.

    A non-zero child status is returned immediately.  If the shared budget is
    exhausted, the active process group is terminated and ``124`` is returned.
    """

    if budget_seconds <= 0:
        raise ValueError("budget_seconds must be positive")
    deadline = time.monotonic() + budget_seconds
    status = _run_commands_parallel(commands, cwd=cwd, deadline=deadline)
    if status != 0:
        if status == EXIT_BUDGET_EXHAUSTED:
            print(
                f"preflight: fast budget exhausted ({budget_seconds:g}s)",
                file=sys.stderr,
            )
        return status
    elapsed = budget_seconds - max(0.0, deadline - time.monotonic())
    print(f"preflight: fast gate passed in {elapsed:.2f}s", file=sys.stderr)
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--budget",
        type=float,
        default=DEFAULT_BUDGET_SECONDS,
        help="total wall-clock budget in seconds (default: 60)",
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    python = sys.executable
    commands = (
        (python, "-m", "ruff", "check", "."),
        (python, "-m", "pyright"),
        (python, "-m", "meridian.dev.pytests"),
    )
    print(f"preflight: fast gate (default testpaths, budget={args.budget:g}s)", file=sys.stderr)
    try:
        return run_commands(commands, cwd=args.repo, budget_seconds=args.budget)
    except KeyboardInterrupt:
        print("preflight: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
