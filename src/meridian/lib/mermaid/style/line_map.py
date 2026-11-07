from __future__ import annotations

from meridian.lib.mermaid.scanner import DiagramTarget


def content_line_to_file_line(target: DiagramTarget, content_line: int) -> int:
    """Convert 1-indexed content line to absolute file line.

    For fenced blocks: file_line = start_line + content_line (fence is start_line).
    For standalone: file_line = content_line (start_line == 1, no fence).
    """
    if target.source == "fenced-block":
        return target.start_line + content_line
    return content_line


__all__ = ["content_line_to_file_line"]
