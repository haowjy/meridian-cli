from __future__ import annotations

import re

from meridian.lib.mermaid.scanner import DiagramTarget
from meridian.lib.mermaid.style.line_map import content_line_to_file_line
from meridian.lib.mermaid.style.preprocess import iter_diagram_lines
from meridian.lib.mermaid.style.types import StyleWarning, WarningCategory

FILL_NO_COLOR_CATEGORY = WarningCategory(
    id="fill-no-color",
    description="Fill without explicit text color",
    default=True,
    diagram_types=None,
)

_STYLE_FILL_RE = re.compile(r"^\s*style\s+(\S+)\s+.*fill:")
_TEXT_COLOR_RE = re.compile(r"(?<![a-z-])color:")
_QUOTED_RE = re.compile(r"(['\"])(?:\\.|(?!\1).)*\1")


def check_fill_no_color(target: DiagramTarget, diagram_type: str | None) -> list[StyleWarning]:
    """Warn on inline style fill declarations without explicit text color."""
    del diagram_type
    warnings: list[StyleWarning] = []
    for content_line, line in iter_diagram_lines(target.content):
        line_without_quotes = _QUOTED_RE.sub("", line)
        match = _STYLE_FILL_RE.search(line_without_quotes)
        if match is None or _TEXT_COLOR_RE.search(line_without_quotes) is not None:
            continue

        node_id = match.group(1)
        warnings.append(
            StyleWarning(
                category=FILL_NO_COLOR_CATEGORY.id,
                file=target.rel,
                line=content_line_to_file_line(target, content_line),
                message=(
                    f"`style {node_id} fill:#...` without `color:` — "
                    "text may be unreadable across light/dark themes"
                ),
                severity="warning",
                suppressed=False,
                suppression_source=None,
            )
        )

    return warnings


__all__ = ["FILL_NO_COLOR_CATEGORY", "check_fill_no_color"]
