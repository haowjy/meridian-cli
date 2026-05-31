#!/usr/bin/env python3
"""Verify built Pi extension bundles are present in Python distributions."""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

_PI_RUNTIME_REL = Path("src/meridian/pi_runtime")
_EXTENSION_SOURCE_REL = _PI_RUNTIME_REL / "extensions"
_EXTENSION_DIST_REL = _PI_RUNTIME_REL / "dist/extensions"
_WHEEL_DIST_PARTS = ("meridian", "pi_runtime", "dist", "extensions")
_SDIST_DIST_PARTS = ("src", "meridian", "pi_runtime", "dist", "extensions")


class VerificationError(RuntimeError):
    """Raised when a distribution misses or adds Pi extension artifacts."""


def discover_expected_extensions(project_root: Path) -> tuple[str, ...]:
    """Return extension names that should produce dist/extensions/<name>/index.js."""

    extension_root = project_root / _EXTENSION_SOURCE_REL
    if not extension_root.is_dir():
        raise VerificationError(f"Missing Pi extension source root: {extension_root}")

    names = tuple(
        sorted(
            child.name
            for child in extension_root.iterdir()
            if child.is_dir() and (child / "src" / "index.ts").is_file()
        )
    )
    if not names:
        raise VerificationError(f"No Pi extension entrypoints found under {extension_root}")
    return names


def verify_local_build(project_root: Path, expected: Sequence[str]) -> None:
    """Check the checkout build output matches the extension source set."""

    dist_root = project_root / _EXTENSION_DIST_REL
    actual = tuple(
        sorted(
            child.name
            for child in dist_root.iterdir()
            if child.is_dir() and (child / "index.js").is_file()
        )
    ) if dist_root.is_dir() else ()
    _assert_exact(
        expected=expected,
        actual=actual,
        label=f"local build output {dist_root}",
    )


def verify_wheel(path: Path, expected: Sequence[str]) -> None:
    with zipfile.ZipFile(path) as archive:
        actual = _extension_names_from_members(archive.namelist(), _WHEEL_DIST_PARTS)
    _assert_exact(expected=expected, actual=actual, label=f"wheel {path}")


def verify_sdist(path: Path, expected: Sequence[str]) -> None:
    with tarfile.open(path) as archive:
        actual = _extension_names_from_members(archive.getnames(), _SDIST_DIST_PARTS)
    _assert_exact(expected=expected, actual=actual, label=f"sdist {path}")


def verify_distributions(dist_dir: Path, project_root: Path) -> None:
    expected = discover_expected_extensions(project_root)
    print("Expected Pi extensions: " + ", ".join(expected))
    verify_local_build(project_root, expected)

    wheels = tuple(sorted(dist_dir.glob("*.whl")))
    sdists = tuple(sorted(dist_dir.glob("*.tar.gz")))
    if not wheels:
        raise VerificationError(f"No wheel files found in {dist_dir}")
    if not sdists:
        raise VerificationError(f"No sdist files found in {dist_dir}")

    for wheel in wheels:
        verify_wheel(wheel, expected)
        print(f"Verified Pi extensions in wheel: {wheel}")
    for sdist in sdists:
        verify_sdist(sdist, expected)
        print(f"Verified Pi extensions in sdist: {sdist}")


def _extension_names_from_members(
    members: Iterable[str],
    dist_parts: tuple[str, ...],
) -> tuple[str, ...]:
    names: set[str] = set()
    for member in members:
        parts = PurePosixPath(member).parts
        for start in range(0, len(parts) - len(dist_parts) - 1):
            if parts[start : start + len(dist_parts)] != dist_parts:
                continue
            tail = parts[start + len(dist_parts) :]
            if len(tail) == 2 and tail[1] == "index.js":
                names.add(tail[0])
    return tuple(sorted(names))


def _assert_exact(*, expected: Sequence[str], actual: Sequence[str], label: str) -> None:
    expected_set = set(expected)
    actual_set = set(actual)
    missing = sorted(expected_set - actual_set)
    unexpected = sorted(actual_set - expected_set)
    if not missing and not unexpected:
        return

    details = [f"Pi extension artifact mismatch in {label}."]
    if missing:
        details.append("Missing: " + ", ".join(missing))
    if unexpected:
        details.append("Unexpected: " + ", ".join(unexpected))
    details.append("Expected exactly: " + ", ".join(expected))
    details.append("Found: " + (", ".join(actual) if actual else "<none>"))
    raise VerificationError("\n".join(details))


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dist_dir",
        nargs="?",
        default="dist",
        type=Path,
        help="Directory containing built .whl and .tar.gz distributions.",
    )
    parser.add_argument(
        "--project-root",
        default=Path.cwd(),
        type=Path,
        help="Repository root used to discover Pi extension sources.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        verify_distributions(
            dist_dir=args.dist_dir.resolve(),
            project_root=args.project_root.resolve(),
        )
    except VerificationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
