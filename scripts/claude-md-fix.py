#!/usr/bin/env python3
"""Create CLAUDE.md mirrors for every AGENTS.md in a directory tree.

Usage:
  python scripts/claude-md-fix.py [ROOT] [--dry-run]

Default ROOT is the current working directory.
A CLAUDE.md is only created when one doesn't already exist.
"""

import os
import sys
from pathlib import Path

SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".meridian",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".agents",
    }
)

MIRROR_CONTENT = "@AGENTS.md\n"


def main() -> None:
    args = sys.argv[1:]
    dry_run = False

    if "--dry-run" in args:
        dry_run = True
        args.remove("--dry-run")

    if len(args) > 1:
        print("Usage: claude-md-fix.py [ROOT] [--dry-run]", file=sys.stderr)
        sys.exit(2)

    root = Path(args[0]).resolve() if args else Path.cwd()

    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    created = 0
    skipped = 0
    conflicts = 0

    for dirpath_str, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        if "AGENTS.md" not in filenames:
            continue

        dirpath = Path(dirpath_str)
        claude_md = dirpath / "CLAUDE.md"

        if claude_md.is_file():
            content = claude_md.read_text(encoding="utf-8").strip()
            if content == "@AGENTS.md":
                skipped += 1
                continue
            rel = claude_md.relative_to(root)
            print(f"[WARN] {rel} exists with different content — skipping", file=sys.stderr)
            conflicts += 1
            continue

        if dry_run:
            rel = claude_md.relative_to(root)
            print(f"[DRY-RUN] would create {rel}")
            created += 1
            continue

        try:
            claude_md.write_text(MIRROR_CONTENT, encoding="utf-8")
        except OSError as exc:
            rel = claude_md.relative_to(root)
            print(f"[ERROR] cannot write {rel}: {exc}", file=sys.stderr)
            conflicts += 1
            continue

        created += 1

    print(f"Created {created}  skipped {skipped}  conflicts {conflicts}")
    if conflicts:
        sys.exit(1)


if __name__ == "__main__":
    main()
