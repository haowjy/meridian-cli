#!/usr/bin/env python3
"""Verify Python distributions use publisher-compatible core metadata."""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

_EXPECTED_METADATA_VERSION = "2.4"


class VerificationError(RuntimeError):
    """Raised when a distribution has an unexpected metadata version."""


def verify_distributions(dist_dir: Path) -> None:
    wheels = tuple(sorted(dist_dir.glob("*.whl")))
    sdists = tuple(sorted(dist_dir.glob("*.tar.gz")))
    if not wheels:
        raise VerificationError(f"No wheel files found in {dist_dir}")
    if not sdists:
        raise VerificationError(f"No sdist files found in {dist_dir}")

    for wheel in wheels:
        version = _wheel_metadata_version(wheel)
        _assert_expected(path=wheel, version=version)
    for sdist in sdists:
        version = _sdist_metadata_version(sdist)
        _assert_expected(path=sdist, version=version)


def _wheel_metadata_version(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        metadata_name = next(
            (name for name in archive.namelist() if name.endswith(".dist-info/METADATA")),
            None,
        )
        if metadata_name is None:
            raise VerificationError(f"Missing wheel METADATA in {path}")
        return _metadata_version(archive.read(metadata_name).decode())


def _sdist_metadata_version(path: Path) -> str:
    with tarfile.open(path) as archive:
        pkg_info_name = next(
            (name for name in archive.getnames() if name.endswith("/PKG-INFO")),
            None,
        )
        if pkg_info_name is None:
            raise VerificationError(f"Missing sdist PKG-INFO in {path}")
        member = archive.extractfile(pkg_info_name)
        if member is None:
            raise VerificationError(f"Unreadable sdist PKG-INFO in {path}")
        return _metadata_version(member.read().decode())


def _metadata_version(contents: str) -> str:
    for line in contents.splitlines():
        if line.startswith("Metadata-Version:"):
            return line.split(":", 1)[1].strip()
    raise VerificationError("Missing Metadata-Version header")


def _assert_expected(*, path: Path, version: str) -> None:
    if version != _EXPECTED_METADATA_VERSION:
        raise VerificationError(
            f"{path} has Metadata-Version {version}; expected {_EXPECTED_METADATA_VERSION}"
        )
    print(f"Verified release metadata in {path}: Metadata-Version {version}")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dist_dir",
        nargs="?",
        default="dist",
        type=Path,
        help="Directory containing built .whl and .tar.gz distributions.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        verify_distributions(args.dist_dir.resolve())
    except VerificationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
