"""Mermaid diagram target collection - file walking and fenced block extraction."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pathspec

from meridian.lib.ignores import SKIP_DIRS, load_ignore_patterns
from meridian.lib.markdown.extract import extract_file

# File extensions to scan
_MERMAID_STANDALONE = frozenset({".mmd", ".mermaid"})
_MARKDOWN_EXT = ".md"


@dataclass
class DiagramTarget:
    """A single mermaid diagram to validate."""

    file: Path  # absolute path to the file
    rel: str  # relative path from scan root (posix)
    content: str  # diagram text content
    start_line: int  # 1-indexed line number where diagram starts
    source: str  # "fenced-block" or "standalone"


def collect_targets(
    path: Path,
    root: Path,
    *,
    depth: int | None = None,
    exclude: list[str] | None = None,
) -> list[DiagramTarget]:
    """Collect all mermaid diagram targets from a file or directory.

    Args:
        path: File or directory to scan
        root: Root directory for relative path calculation
        depth: Maximum directory depth (None = unlimited, 0 = root only)
        exclude: Additional glob patterns to exclude

    Returns:
        List of DiagramTarget objects sorted by file path, then line number.
    """
    path = path.resolve()
    root = root.resolve()

    if path.is_file():
        targets = _collect_from_file(path, root)
    else:
        targets = _collect_from_directory(path, root, depth=depth, exclude=exclude)

    # Sort by file path, then line number
    return sorted(targets, key=lambda t: (t.rel, t.start_line))


def _collect_from_file(file_path: Path, root: Path) -> list[DiagramTarget]:
    """Collect targets from a single file."""
    suffix = file_path.suffix.lower()
    rel = _rel_posix(file_path, root)

    if suffix in _MERMAID_STANDALONE:
        # Standalone mermaid file - entire content is one diagram
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [
            DiagramTarget(
                file=file_path,
                rel=rel,
                content=content,
                start_line=1,
                source="standalone",
            )
        ]

    if suffix == _MARKDOWN_EXT:
        # Markdown file - extract fenced mermaid blocks
        return _extract_mermaid_blocks(file_path, root)

    return []


def _extract_mermaid_blocks(md_path: Path, root: Path) -> list[DiagramTarget]:
    """Extract mermaid fenced blocks from a markdown file."""
    doc = extract_file(md_path)
    if doc.error:
        return []

    rel = _rel_posix(md_path, root)
    targets: list[DiagramTarget] = []

    for block in doc.fenced_blocks:
        # Check if language starts with "mermaid" (case-insensitive)
        # Handles cases like "mermaid title="Flow"" or "Mermaid"
        lang_parts = block.language.split()
        if not lang_parts:
            continue
        first_token = lang_parts[0].lower()
        if first_token != "mermaid":
            continue

        targets.append(
            DiagramTarget(
                file=md_path,
                rel=rel,
                content=block.content,
                start_line=block.start_line,
                source="fenced-block",
            )
        )

    return targets


def _collect_from_directory(
    dir_path: Path,
    root: Path,
    *,
    depth: int | None,
    exclude: list[str] | None,
) -> list[DiagramTarget]:
    """Walk a directory and collect all mermaid targets."""
    # Load .mermaidignore patterns
    mermaidignore_spec = load_ignore_patterns(dir_path, ".mermaidignore")

    # Combine with --exclude patterns
    exclude_spec: pathspec.PathSpec[Any] | None = None
    if exclude:
        exclude_spec = cast(
            "pathspec.PathSpec[Any]",
            pathspec.PathSpec.from_lines("gitwildmatch", exclude),  # pyright: ignore[reportUnknownMemberType]
        )

    targets: list[DiagramTarget] = []

    for dirpath_str, dirnames, filenames in os.walk(dir_path):
        dirpath = Path(dirpath_str)

        # Compute current depth relative to scan root
        try:
            rel_dir = dirpath.relative_to(dir_path)
            current_depth = len(rel_dir.parts)
        except ValueError:
            current_depth = 0

        # Check depth limit
        if depth is not None and current_depth > depth:
            dirnames.clear()  # Don't descend further
            continue

        # Prune skipped directories in-place
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for filename in filenames:
            file_path = dirpath / filename
            suffix = file_path.suffix.lower()

            # Only process markdown and standalone mermaid files
            if suffix != _MARKDOWN_EXT and suffix not in _MERMAID_STANDALONE:
                continue

            # Apply ignore patterns
            rel = _rel_posix(file_path, dir_path)
            if mermaidignore_spec and mermaidignore_spec.match_file(rel):
                continue
            if exclude_spec and exclude_spec.match_file(rel):
                continue

            # Collect targets from this file
            targets.extend(_collect_from_file(file_path, root))

    return targets


def _rel_posix(path: Path, root: Path) -> str:
    """Get path relative to root as forward-slash string."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = ["DiagramTarget", "collect_targets"]
