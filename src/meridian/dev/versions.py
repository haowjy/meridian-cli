"""The places a release version is written, and the check that they agree.

`__init__.py` is read by hatch, the `meridian-sync` pin by installers, and `Cargo.toml` by
maturin; `Cargo.lock` repeats the crate's version. The release step writes through
`LOCATIONS` and the `versions` lint reads through it, so a location added here is both
written and checked.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.platform.atomic import atomic_write_text

REPO_ROOT = Path(__file__).resolve().parents[3]

_PYTHON_RC = re.compile(r"^(\d+\.\d+\.\d+)rc(\d+)$")


@dataclass(frozen=True)
class Location:
    """One file holding the version: `pattern`'s first match, group 1 is the version."""

    path: str
    pattern: re.Pattern[str]
    # Cargo spells a release candidate `0.24.0-rc.1`; Python spells it `0.24.0rc1`.
    cargo_spelling: bool = False

    def spell(self, python_version: str) -> str:
        rc = _PYTHON_RC.match(python_version)
        if self.cargo_spelling and rc:
            return f"{rc[1]}-rc.{rc[2]}"
        return python_version

    def read(self, root: Path) -> str | None:
        match = self.pattern.search((root / self.path).read_text(encoding="utf-8"))
        return match[1] if match else None

    def write(self, root: Path, python_version: str) -> None:
        path = root / self.path
        text = path.read_text(encoding="utf-8")
        match = self.pattern.search(text)
        if match is None:
            raise ValueError(f"{self.path}: no version line matches {self.pattern.pattern!r}")
        version = self.spell(python_version)
        atomic_write_text(path, text[: match.start(1)] + version + text[match.end(1) :])


LOCATIONS: tuple[Location, ...] = (
    Location("src/meridian/__init__.py", re.compile(r'^__version__ = "([^"]+)"$', re.M)),
    Location(
        "pyproject.toml",
        re.compile(r'^  "meridian-sync==([^;"]+); sys_platform != \'win32\'",$', re.M),
    ),
    Location(
        "crates/meridian-sync/Cargo.toml",
        re.compile(r'^\[package\]\n(?:(?!\[).*\n)*?version = "([^"]+)"$', re.M),
        cargo_spelling=True,
    ),
    Location(
        "crates/meridian-sync/Cargo.lock",
        re.compile(r'^name = "meridian-sync"\nversion = "([^"]+)"$', re.M),
        cargo_spelling=True,
    ),
)


def disagreements(root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """`(path, message)` for each location whose version differs from `__init__.py`'s."""
    python_version = LOCATIONS[0].read(root)
    if python_version is None:
        return [(LOCATIONS[0].path, "no version line found")]
    problems: list[tuple[str, str]] = []
    for location in LOCATIONS[1:]:
        found = location.read(root)
        expected = location.spell(python_version)
        if found != expected:
            problems.append(
                (location.path, f"version is {found}, but {LOCATIONS[0].path} says {expected}")
            )
    return problems


def write_all(python_version: str, root: Path = REPO_ROOT) -> None:
    """Write the version everywhere, then prove every location holds it."""
    for location in LOCATIONS:
        location.write(root, python_version)
    for location in LOCATIONS:
        if location.read(root) != location.spell(python_version):
            raise ValueError(f"{location.path}: version did not land")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write", help="write a version to every location")
    write.add_argument("python_version", help="X.Y.Z or X.Y.ZrcN")
    commands.add_parser("paths", help="print every location's path, one per line")
    args = parser.parse_args(argv)
    if args.command == "paths":
        print("\n".join(location.path for location in LOCATIONS))
        return 0
    try:
        write_all(args.python_version)
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
