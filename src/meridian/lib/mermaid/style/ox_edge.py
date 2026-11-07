from __future__ import annotations

import re

from meridian.lib.mermaid.scanner import DiagramTarget
from meridian.lib.mermaid.style.line_map import content_line_to_file_line
from meridian.lib.mermaid.style.preprocess import iter_diagram_lines
from meridian.lib.mermaid.style.types import StyleWarning, WarningCategory

OX_EDGE_CATEGORY = WarningCategory(
    id="ox-edge",
    description="Suspicious circle/cross edge token",
    default=True,
    diagram_types=frozenset({"flowchart"}),
)

_OX_EDGE_RE = re.compile(r"---[ox]([a-z])")


def check_ox_edge(target: DiagramTarget, diagram_type: str | None) -> list[StyleWarning]:
    """Warn when a flowchart edge target may be parsed as circle/cross edge syntax."""
    if diagram_type != "flowchart":
        return []

    warnings: list[StyleWarning] = []
    for content_line, line in iter_diagram_lines(target.content):
        for match in _OX_EDGE_RE.finditer(line):
            token = _edge_token(line, match.start())
            warnings.append(
                StyleWarning(
                    category=OX_EDGE_CATEGORY.id,
                    file=target.rel,
                    line=content_line_to_file_line(target, content_line),
                    message=(
                        f"edge `{token}` may be parsed as circle-edge ending; "
                        f"add space before '{match.group(1)}' if target node is intended"
                    ),
                    severity="warning",
                    suppressed=False,
                    suppression_source=None,
                )
            )
    return warnings


def _edge_token(line: str, start: int) -> str:
    end = start
    while end < len(line) and not line[end].isspace():
        end += 1
    return line[start:end]


__all__ = ["OX_EDGE_CATEGORY", "check_ox_edge"]
