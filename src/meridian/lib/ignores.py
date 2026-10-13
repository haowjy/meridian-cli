"""Shared gitignore-style pattern loading for kg and mermaid validators."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pathspec

if TYPE_CHECKING:
    from pathspec.pattern import Pattern


def compile_gitwildmatch(patterns: list[str]) -> pathspec.PathSpec[Pattern]:
    """Compile gitwildmatch patterns behind pathspec's incomplete type boundary."""
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)  # pyright: ignore[reportUnknownMemberType]  # pathspec leaves this factory incompletely typed.


def load_ignore_patterns(root: Path, filename: str) -> pathspec.PathSpec[Pattern] | None:
    """Load gitignore-style patterns from a file.

    Args:
        root: Directory containing the ignore file
        filename: Name of the ignore file (e.g., ".kgignore", ".mermaidignore")

    Returns:
        Compiled PathSpec if file exists and has patterns, None otherwise.
    """
    ignore_file = root / filename
    if not ignore_file.exists():
        return None

    lines = ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
    patterns = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    if not patterns:
        return None
    return compile_gitwildmatch(patterns)


__all__ = ["compile_gitwildmatch", "load_ignore_patterns"]
