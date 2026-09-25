"""Explicit -I -S entrypoint for isolated continue-replay acceptance."""

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--dependency-root", type=Path, required=True)
    args = parser.parse_args()
    source = args.source_root
    sys.path.insert(0, str(source / "tests/acceptance"))
    from acceptance_bootstrap import run_continue_replay

    run_continue_replay(source, args.dependency_root)


if __name__ == "__main__":
    main()
