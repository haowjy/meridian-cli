"""Architecture-check lane runner.

Runs source-drift and architecture contract checks separately from the broad
behavioral pytest lane.
"""

from __future__ import annotations

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

ARCHITECTURE_TEST_TARGETS: tuple[str, ...] = (
    "tests/unit/core/test_architecture_contracts.py",
    "tests/contract/launch/test_launch_factory.py",
)


def build_pytest_args(argv: Sequence[str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        *DEFAULT_ARGS,
        *ARCHITECTURE_TEST_TARGETS,
        *argv,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    user_args = list(sys.argv[1:] if argv is None else argv)
    completed = subprocess.run(build_pytest_args(user_args), check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
