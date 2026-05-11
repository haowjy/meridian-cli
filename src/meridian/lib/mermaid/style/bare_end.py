"""Bare lowercase end flowchart style check."""

from __future__ import annotations

import re

from meridian.lib.mermaid.scanner import DiagramTarget
from meridian.lib.mermaid.style.line_map import content_line_to_file_line
from meridian.lib.mermaid.style.preprocess import iter_diagram_lines
from meridian.lib.mermaid.style.types import StyleWarning, WarningCategory

BARE_END_CATEGORY = WarningCategory(
    id="bare-end",
    description="Bare lowercase end in flowchart",
    default=True,
    diagram_types=frozenset({"flowchart"}),
)

_END_RE = re.compile(r"\bend\b")
_QUOTED_RE = re.compile(r"(['\"])(?:\\.|(?!\1).)*\1")


def check_bare_end(target: DiagramTarget, diagram_type: str | None) -> list[StyleWarning]:
    """Warn on bare lowercase end used where Mermaid may read a block terminator."""
    if diagram_type != "flowchart":
        return []

    warnings: list[StyleWarning] = []
    for content_line, stripped in iter_diagram_lines(target.content):
        if not stripped:
            continue
        if stripped == "end":
            continue

        line_without_quotes = _QUOTED_RE.sub("", stripped)
        if _END_RE.search(line_without_quotes) is None:
            continue

        warnings.append(
            StyleWarning(
                category=BARE_END_CATEGORY.id,
                file=target.rel,
                line=content_line_to_file_line(target, content_line),
                message=(
                    'bare lowercase `end` in flowchart may break parser; use `End` or `["end"]`'
                ),
                severity="warning",
                suppressed=False,
                suppression_source=None,
            )
        )
    return warnings


__all__ = ["BARE_END_CATEGORY", "check_bare_end"]
