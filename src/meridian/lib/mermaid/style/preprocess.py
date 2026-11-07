from __future__ import annotations

from collections.abc import Iterator


def iter_diagram_lines(content: str) -> Iterator[tuple[int, str]]:
    """Yield original content line numbers and stripped diagram body lines.

    Skips YAML frontmatter at the start of a diagram, Mermaid directive blocks,
    and comment-only lines. Line numbers remain relative to the original content
    so callers can map warnings back to file lines.
    """
    lines = content.split("\n")
    frontmatter_end = _frontmatter_end_line(lines)
    in_directive = False

    for content_line, raw_line in enumerate(lines, start=1):
        if frontmatter_end is not None and content_line <= frontmatter_end:
            continue

        stripped = raw_line.rstrip("\r").strip()

        if in_directive:
            if stripped.endswith("}%%"):
                in_directive = False
            continue

        if stripped.startswith("%%{"):
            if not stripped.endswith("}%%"):
                in_directive = True
            continue

        if stripped.startswith("%%"):
            continue

        yield content_line, stripped


def _frontmatter_end_line(lines: list[str]) -> int | None:
    """Return ending line number for leading YAML frontmatter, if present."""
    first_content_index: int | None = None
    for index, line in enumerate(lines):
        if line.strip():
            first_content_index = index
            break

    if first_content_index is None or lines[first_content_index].strip() != "---":
        return None

    for index in range(first_content_index + 1, len(lines)):
        if lines[index].strip() == "---":
            return index + 1

    return None


__all__ = ["iter_diagram_lines"]
