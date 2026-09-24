"""Selected-lineage projection for an exact Pi native JSONL journal.

Pi appends a tree-shaped journal. The last persisted leaf directive (or the last
entry when no directive follows) is Pi's reopen-default position; it is not
evidence of a process's live in-memory leaf.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, cast

PiViewBasis = Literal["reopen-default"]

_KNOWN_ENTRY_TYPES = frozenset(
    {
        "message",
        "compaction",
        "branch_summary",
        "custom_message",
        "model_change",
        "thinking_level_change",
        "custom",
        "label",
        "session_info",
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "bash_execution",
        "text_delta",
        "image_delta",
        "agent_start",
        "agent_end",
        "agent_error",
        "agent_message_start",
        "agent_message_end",
        "agent_message_delta",
        "agent_tool_start",
        "agent_tool_end",
        "agent_tool_update",
        "agent_tool_error",
        "agent_tool_result",
        "agent_tool_call",
        "agent_tool_call_result",
        "agent_thinking_start",
        "agent_thinking_delta",
        "agent_thinking_end",
        "agent_text_start",
        "agent_text_delta",
        "agent_text_end",
        "agent_image_start",
        "agent_image_delta",
        "agent_image_end",
    }
)


@dataclass(frozen=True)
class PiJournalProjection:
    """Raw selected Pi rows plus an explicit view and source-completeness result."""

    events: tuple[dict[str, object], ...]
    view_basis: PiViewBasis
    complete: bool
    reasons: tuple[str, ...]


def project_pi_reopen_default(source: str) -> PiJournalProjection:
    """Select the exact journal's persisted reopen-default root-to-leaf lineage.

    The caller supplies bytes decoded as UTF-8 from the already-authorized Pi
    source. This pure projector deliberately does not discover or open files.
    It preserves source order among selected ancestors, while excluding sibling
    branches. Incomplete sources are returned with reasons, never represented as
    complete empty transcripts.
    """
    reasons: list[str] = []
    if source and not source.endswith("\n"):
        reasons.append("torn partial line")
        source = source[: source.rfind("\n") + 1]

    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            reasons.append(f"malformed row at line {line_number}")
            continue
        if not isinstance(value, dict):
            reasons.append(f"unknown row at line {line_number}")
            continue
        rows.append(cast("dict[str, object]", value))

    if not rows or rows[0].get("type") != "session":
        return PiJournalProjection(
            (), "reopen-default", False, tuple([*reasons, "missing Pi session header"])
        )
    header = rows[0]
    if not isinstance(header.get("id"), str) or "cwd" not in header:
        reasons.append("invalid Pi session header")
    if type(header.get("version", 1)) is not int or header.get("version", 1) not in (1, 2, 3):
        reasons.append("unsupported Pi session version")

    entries: dict[str, tuple[int, dict[str, object]]] = {}
    leaf_id: str | None = None
    for index, row in enumerate(rows[1:]):
        entry_type = row.get("type")
        if not isinstance(entry_type, str) or not isinstance(row.get("id"), str):
            reasons.append(f"unknown row at line {index + 2}")
            continue
        if entry_type == "leaf":
            target = row.get("targetId")
            if target is not None and not isinstance(target, str):
                reasons.append(f"invalid leaf target at line {index + 2}")
                continue
            leaf_id = target
            continue
        parent = row.get("parentId")
        if "parentId" not in row or (parent is not None and not isinstance(parent, str)):
            reasons.append(f"invalid parent at line {index + 2}")
            continue
        entry_id = cast("str", row["id"])
        if entry_id in entries:
            reasons.append(f"duplicate entry id: {entry_id}")
            continue
        if entry_type not in _KNOWN_ENTRY_TYPES:
            reasons.append(f"unknown row type: {entry_type}")
        entries[entry_id] = (index + 1, row)
        # JsonlSessionStorage updates its leaf from every non-leaf row.
        leaf_id = entry_id

    selected: list[tuple[int, dict[str, object]]] = []
    visited: set[str] = set()
    current = leaf_id
    while current is not None:
        if current in visited:
            reasons.append("cycle in Pi parent chain")
            break
        visited.add(current)
        found = entries.get(current)
        if found is None:
            reasons.append(f"missing parent or leaf entry: {current}")
            break
        index, row = found
        selected.append((index, row))
        parent = row.get("parentId")
        current = parent if isinstance(parent, str) else None

    selected.sort(key=lambda item: item[0])
    return PiJournalProjection(
        (header, *(row for _, row in selected)),
        "reopen-default",
        not reasons,
        tuple(dict.fromkeys(reasons)),
    )
