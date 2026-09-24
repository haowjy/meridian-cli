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


@dataclass(frozen=True)
class PiJournalProjection:
    """Raw selected Pi rows plus an explicit view and source-completeness result."""

    events: tuple[dict[str, object], ...]
    view_basis: PiViewBasis
    complete: bool
    reasons: tuple[str, ...]


def project_pi_reopen_default(source: str) -> PiJournalProjection:
    """Project the installed Pi 0.87.1 legacy-v3 reader's persisted branch.

    The caller supplies the exact already-authorized source. Complete final
    JSON rows are accepted without a trailing LF, as in SessionManager. This
    pure projector neither discovers nor opens files.
    """
    reasons: list[str] = []
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
    if header.get("version") != 3 or type(header.get("version")) is not int:
        reasons.append("unsupported Pi native dialect; expected legacy v3")

    entries: dict[str, dict[str, object]] = {}
    leaf_id: str | None = None
    for line_number, row in enumerate(rows[1:], start=2):
        entry_type = row.get("type")
        entry_id = row.get("id")
        if entry_type == "leaf":
            reasons.append(f"unsupported Pi native leaf directive at line {line_number}")
            continue
        if not isinstance(entry_type, str) or not isinstance(entry_id, str) or not entry_id:
            reasons.append(f"invalid Pi entry identity at line {line_number}")
            continue
        parent = row.get("parentId")
        if "parentId" not in row or (parent is not None and not isinstance(parent, str)):
            reasons.append(f"invalid parent at line {line_number}")
            continue
        if entry_id in entries:
            reasons.append(f"duplicate entry id: {entry_id}")
            continue
        if entry_type not in PI_JOURNAL_ENTRY_TYPES:
            reasons.append(f"unknown row type: {entry_type}")
        entries[entry_id] = row
        # SessionManager._buildIndex sets leaf to every non-session entry.
        leaf_id = entry_id

    leaf_to_root: list[dict[str, object]] = []
    visited: set[str] = set()
    current = leaf_id
    while current is not None:
        if current in visited:
            reasons.append("cycle in Pi parent chain")
            break
        visited.add(current)
        row = entries.get(current)
        if row is None:
            reasons.append(f"missing parent or leaf entry: {current}")
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
