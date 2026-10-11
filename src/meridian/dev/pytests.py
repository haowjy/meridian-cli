"""Token-efficient pytest wrapper for agent spawns."""

import os
import subprocess
import sys
from collections.abc import Sequence

DEFAULT_ARGS: tuple[str, ...] = (
    "-q",
    "--tb=line",
    "--show-capture=no",
    "--disable-warnings",
    "--maxfail=1",
    "-r",
    "fE",
    "--force-short-summary",
)
LAST_FAILED_ARGS: tuple[str, ...] = ("--lf", "--lfnf=all")


def _is_truthy_env(name: str) -> bool:
    value = os.getenv(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_pytest_args(
    argv: Sequence[str],
    *,
    include_last_failed: bool,
) -> list[str]:
    args = [sys.executable, "-m", "pytest", *DEFAULT_ARGS]
    if include_last_failed:
        args.extend(LAST_FAILED_ARGS)
    args.extend(argv)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    user_args = list(sys.argv[1:] if argv is None else argv)
    selection = (
        f"explicit pytest arguments: {' '.join(user_args)}"
        if user_args
        else "default fast paths from pyproject.toml (unless pytest options override them)"
    )
    print(f"pytest-llm: {selection}", file=sys.stderr)
    command = build_pytest_args(
        user_args, include_last_failed=_is_truthy_env("PYTESTS_LAST_FAILED")
    )
    completed = subprocess.run(command, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
