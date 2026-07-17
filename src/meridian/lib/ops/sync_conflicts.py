"""Sync conflict query and resolution operations."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.config.context_config import ContextConfig
from meridian.lib.context.resolver import resolve_context_paths
from meridian.lib.core.util import FormatContext
from meridian.lib.hooks.builtin.autosync_store import (
    ConflictRecord,
    find_conflict_by_id,
    has_autosync_state,
    read_unresolved_conflicts,
    transaction,
)
from meridian.lib.ops.runtime import resolve_runtime_authority_for_read
from meridian.lib.state.paths import load_context_config


class ConflictEntry(BaseModel):
    """One conflict record."""

    model_config = ConfigDict(frozen=True)

    id: str
    context: str
    sync_root: str
    conflict_type: str
    paths: list[str]
    local_sha: str
    remote_sha: str
    remote_branch: str
    created_at: str
    resolved: bool


def _empty_conflicts() -> list[ConflictEntry]:
    return []


class ConflictListOutput(BaseModel):
    """Output for conflict list operation."""

    model_config = ConfigDict(frozen=True)

    conflicts: list[ConflictEntry] = Field(default_factory=_empty_conflicts)

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        unresolved = [conflict for conflict in self.conflicts if not conflict.resolved]
        if not unresolved:
            return "No unresolved conflicts."

        lines: list[str] = ["Conflicts:"]
        for conflict in unresolved:
            paths_str = ", ".join(conflict.paths) if conflict.paths else "(no paths)"
            lines.append(f"  {conflict.id} {paths_str} ({conflict.conflict_type})")
        return "\n".join(lines)


class ConflictShowOutput(BaseModel):
    """Output for conflict show operation."""

    model_config = ConfigDict(frozen=True)

    conflict: ConflictEntry | None = None
    error: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if self.error:
            return self.error
        if self.conflict is None:
            return "Conflict not found."

        conflict = self.conflict
        paths_str = ", ".join(conflict.paths) if conflict.paths else "(no paths)"
        lines = [
            f"{conflict.id} {paths_str}",
            f"  type: {conflict.conflict_type}",
            f"  local: {conflict.local_sha[:7]}  remote: {conflict.remote_sha[:7]}",
            f"  remote branch: origin/{conflict.remote_branch}",
            f"  created: {conflict.created_at}",
            "",
            "Resolve:",
            f"  git fetch origin && git merge origin/{conflict.remote_branch}",
            "  # resolve conflicts, then: git add <files> && git commit",
            f"  meridian sync conflict resolve {conflict.id}",
        ]
        return "\n".join(lines)


class ConflictResolveOutput(BaseModel):
    """Output for conflict resolve operation."""

    model_config = ConfigDict(frozen=True)

    conflict_id: str
    resolved: bool
    error: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if self.error:
            return f"Error resolving {self.conflict_id}: {self.error}"
        return f"Resolved {self.conflict_id}."


def _to_entry(record: ConflictRecord) -> ConflictEntry:
    return ConflictEntry(
        id=record.id,
        context=record.context,
        sync_root=record.sync_root,
        conflict_type=record.conflict_type,
        paths=list(record.paths),
        local_sha=record.local_sha,
        remote_sha=record.remote_sha,
        remote_branch=record.remote_branch,
        created_at=record.created_at,
        resolved=record.resolved,
    )


def _find_sync_roots() -> list[Path]:
    """Find sync roots by checking resolved context paths."""

    try:
        authority = resolve_runtime_authority_for_read()
    except Exception:
        return []

    context_config = load_context_config(authority.project_root) or ContextConfig()
    resolved_paths = resolve_context_paths(authority.project_root, context_config)

    candidates = [resolved_paths.work_root, resolved_paths.kb_root]
    candidates.extend(path for path, _source in resolved_paths.extra.values())

    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if has_autosync_state(candidate):
            roots.append(candidate)
    return roots


def list_conflicts_sync() -> ConflictListOutput:
    """List unresolved conflicts across all sync roots."""

    conflicts: list[ConflictEntry] = []
    for sync_root in _find_sync_roots():
        conflicts.extend(_to_entry(record) for record in read_unresolved_conflicts(sync_root))
    return ConflictListOutput(conflicts=conflicts)


def show_conflict_sync(conflict_id: str) -> ConflictShowOutput:
    """Show details for one conflict id."""

    roots = _find_sync_roots()
    _root, record = find_conflict_by_id(roots, conflict_id)
    if record is None:
        return ConflictShowOutput(error=f"Conflict '{conflict_id}' not found.")
    return ConflictShowOutput(conflict=_to_entry(record))


def resolve_conflict_sync(conflict_id: str) -> ConflictResolveOutput:
    """Mark a conflict as resolved within its autosync transaction."""

    roots = _find_sync_roots()
    sync_root, record = find_conflict_by_id(roots, conflict_id)
    if record is None:
        # Idempotent: treat missing conflict as already resolved.
        return ConflictResolveOutput(conflict_id=conflict_id, resolved=True)

    if sync_root is None:
        return ConflictResolveOutput(
            conflict_id=conflict_id,
            resolved=False,
            error="Could not find sync root for conflict.",
        )

    try:
        with transaction(sync_root) as autosync_tx:
            resolved = autosync_tx.mark_resolved(conflict_id)
    except (OSError, TimeoutError) as exc:
        return ConflictResolveOutput(
            conflict_id=conflict_id,
            resolved=False,
            error=f"Failed to acquire autosync transaction: {exc}",
        )
    if not resolved:
        return ConflictResolveOutput(
            conflict_id=conflict_id,
            resolved=False,
            error=f"Failed to mark conflict {conflict_id} as resolved.",
        )

    return ConflictResolveOutput(
        conflict_id=conflict_id,
        resolved=True,
    )


__all__ = [
    "ConflictEntry",
    "ConflictListOutput",
    "ConflictResolveOutput",
    "ConflictShowOutput",
    "list_conflicts_sync",
    "resolve_conflict_sync",
    "show_conflict_sync",
]
