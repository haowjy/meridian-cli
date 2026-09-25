"""Selected-lineage projection for a qualified Pi 0.87.1 legacy-v3 journal.

This supports the installed SessionManager's v3 reopen behavior only: each
entry row replaces the leaf, and parentId is followed to the root. A v3 header
alone is not dialect evidence; leaf directives and unknown entry types are
refused rather than interpreted using another Pi storage implementation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, cast

from meridian.lib.harness.transcript import PI_JOURNAL_ENTRY_TYPES

PiViewBasis = Literal["reopen-default"]
PiCompletenessReason = Literal[
    "malformed_row",
    "unknown_row",
    "missing_header",
    "invalid_header",
    "unsupported_dialect",
    "unsupported_leaf",
    "invalid_entry",
    "invalid_parent",
    "duplicate_id",
    "unknown_type",
    "missing_parent",
    "cycle",
    "invalid_model_change",
    "invalid_message",
]


@dataclass(frozen=True)
class PiJournalProjection:
    """Raw selected Pi rows plus an explicit view and source-completeness result."""

    events: tuple[dict[str, object], ...]
    view_basis: PiViewBasis
    complete: bool
    reasons: tuple[PiCompletenessReason, ...]


def project_pi_reopen_default(source: str) -> PiJournalProjection:
    """Project the installed Pi 0.87.1 legacy-v3 reader's persisted branch.

    The caller supplies the exact already-authorized source. Complete final
    JSON rows are accepted without a trailing LF, as in SessionManager. This
    pure projector neither discovers nor opens files.
    """
    reasons: list[PiCompletenessReason] = []
    rows: list[dict[str, object]] = []
    for _line_number, line in enumerate(source.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            reasons.append("malformed_row")
            continue
        if not isinstance(value, dict):
            reasons.append("unknown_row")
            continue
        rows.append(cast("dict[str, object]", value))

    if not rows or rows[0].get("type") != "session":
        return PiJournalProjection(
            (), "reopen-default", False, tuple(dict.fromkeys([*reasons, "missing_header"]))
        )
    header = rows[0]
    if not isinstance(header.get("id"), str) or "cwd" not in header:
        reasons.append("invalid_header")
    if header.get("version") != 3 or type(header.get("version")) is not int:
        reasons.append("unsupported_dialect")

    entries: dict[str, dict[str, object]] = {}
    leaf_id: str | None = None
    for _line_number, row in enumerate(rows[1:], start=2):
        entry_type = row.get("type")
        entry_id = row.get("id")
        if entry_type == "leaf":
            reasons.append("unsupported_leaf")
            continue
        if not isinstance(entry_type, str) or not isinstance(entry_id, str) or not entry_id:
            reasons.append("invalid_entry")
            continue
        parent = row.get("parentId")
        if "parentId" not in row or (parent is not None and not isinstance(parent, str)):
            reasons.append("invalid_parent")
            continue
        if entry_id in entries:
            reasons.append("duplicate_id")
            continue
        if entry_type not in PI_JOURNAL_ENTRY_TYPES:
            reasons.append("unknown_type")
        entries[entry_id] = row
        if entry_type == "model_change":
            provider, model_id = row.get("provider"), row.get("modelId")
            if (
                not isinstance(provider, str)
                or not provider.strip()
                or not isinstance(model_id, str)
                or not model_id.strip()
            ):
                reasons.append("invalid_model_change")
        if entry_type == "message":
            message = row.get("message")
            if not isinstance(message, dict):
                reasons.append("invalid_message")
            else:
                payload = cast("dict[str, object]", message)
                role = payload.get("role")
                provider, model = payload.get("provider"), payload.get("model")
                if not isinstance(role, str) or (
                    role == "assistant"
                    and (
                        not isinstance(provider, str)
                        or not provider.strip()
                        or not isinstance(model, str)
                        or not model.strip()
                    )
                ):
                    reasons.append("invalid_message")
        # SessionManager._buildIndex sets leaf to every non-session entry.
        leaf_id = entry_id

    # Validate the full parent graph in linear time; selection below still
    # folds settings only along the reopen-default leaf ancestry.
    for row in entries.values():
        parent = row.get("parentId")
        if isinstance(parent, str) and parent not in entries:
            reasons.append("missing_parent")
    colors: dict[str, int] = {}
    for start in entries:
        if colors.get(start, 0):
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                colors[node] = 2
                continue
            color = colors.get(node, 0)
            if color == 1:
                reasons.append("cycle")
                continue
            if color == 2:
                continue
            colors[node] = 1
            stack.append((node, True))
            parent = entries[node].get("parentId")
            if isinstance(parent, str) and parent in entries:
                stack.append((parent, False))

    leaf_to_root: list[dict[str, object]] = []
    visited: set[str] = set()
    current = leaf_id
    while current is not None:
        if current in visited:
            reasons.append("cycle")
            break
        visited.add(current)
        row = entries.get(current)
        if row is None:
            reasons.append("missing_parent")
            break
        leaf_to_root.append(row)
        parent = row.get("parentId")
        current = parent if isinstance(parent, str) else None

    return PiJournalProjection(
        (header, *reversed(leaf_to_root)),
        "reopen-default",
        not reasons,
        tuple(dict.fromkeys(reasons)),
    )
