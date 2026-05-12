"""Shared gitignore-style pattern loading for kg and mermaid validators."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pathspec


def load_ignore_patterns(root: Path, filename: str) -> pathspec.PathSpec[Any] | None:
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
    return cast(
        "pathspec.PathSpec[Any]",
        pathspec.PathSpec.from_lines("gitwildmatch", patterns),  # pyright: ignore[reportUnknownMemberType] - third-party overload has incomplete typing.
    )


__all__ = ["load_ignore_patterns"]
