"""Sync conflict query and resolution operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.config.context_config import ContextConfig
from meridian.lib.context.resolver import resolve_context_paths
from meridian.lib.core.util import FormatContext
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


class ConflictListOutput(BaseModel):
    """Output for conflict list operation."""

    model_config = ConfigDict(frozen=True)

    conflicts: list[ConflictEntry] = Field(default_factory=list)

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
    notice_removed: bool = False
    error: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if self.error:
            return f"Error resolving {self.conflict_id}: {self.error}"
        parts = [f"Resolved {self.conflict_id}."]
        if self.notice_removed:
            parts.append("AGENTS.md notice removed.")
        return " ".join(parts)


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
        conflict_dir = candidate / ".meridian" / "autosync" / "conflicts"
        state_file = candidate / ".meridian" / "autosync" / "state.json"
        if conflict_dir.exists() or state_file.exists():
            roots.append(candidate)
    return roots


def _conflict_dir(sync_root: Path) -> Path:
    return sync_root / ".meridian" / "autosync" / "conflicts"


def _read_conflicts(sync_root: Path) -> list[ConflictEntry]:
    """Read conflict metadata for one sync root."""

    conflict_dir = _conflict_dir(sync_root)
    if not conflict_dir.exists():
        return []

    try:
        files = sorted(conflict_dir.iterdir())
    except OSError:
        return []

    conflicts: list[ConflictEntry] = []
    for file_path in files:
        if file_path.suffix != ".json":
            continue
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if not isinstance(data, dict):
            continue

        conflict_id = data.get("id")
        if not isinstance(conflict_id, str) or not conflict_id.strip():
            conflict_id = file_path.stem

        paths_raw = data.get("paths", [])
        paths = [str(item) for item in paths_raw] if isinstance(paths_raw, list) else []

        conflicts.append(
            ConflictEntry(
                id=conflict_id,
                context=str(data.get("context", "unknown")),
                sync_root=str(data.get("sync_root", sync_root.as_posix())),
                conflict_type=str(data.get("conflict_type", "unknown")),
                paths=paths,
                local_sha=str(data.get("local_sha", "unknown")),
                remote_sha=str(data.get("remote_sha", "unknown")),
                remote_branch=str(data.get("remote_branch", "main")),
                created_at=str(data.get("created_at", "unknown")),
                resolved=bool(data.get("resolved", False)),
            )
        )
    return conflicts


def _find_conflict_by_id(conflict_id: str) -> tuple[Path | None, ConflictEntry | None]:
    """Find one conflict across all known sync roots."""

    for sync_root in _find_sync_roots():
        for conflict in _read_conflicts(sync_root):
            if conflict.id == conflict_id:
                return sync_root, conflict
    return None, None


def _strip_agents_notice(sync_root: Path, conflict_id: str) -> bool:
    """Remove a managed autosync conflict notice block from AGENTS.md."""

    agents_md = sync_root / "AGENTS.md"
    if not agents_md.exists():
        return False

    try:
        content = agents_md.read_text(encoding="utf-8")
    except OSError:
        return False

    start_marker = f"<!-- autosync-conflict:{conflict_id} -->"
    end_marker = f"<!-- /autosync-conflict:{conflict_id} -->"

    start_idx = content.find(start_marker)
    end_start_idx = content.find(end_marker)
    if start_idx < 0 or end_start_idx < 0 or end_start_idx < start_idx:
        return False

    end_idx = end_start_idx + len(end_marker)
    if end_idx < len(content) and content[end_idx] == "\n":
        end_idx += 1

    new_content = content[:start_idx] + content[end_idx:]

    notices_start = "<!-- autosync-notices -->"
    notices_end = "<!-- /autosync-notices -->"
    ns_idx = new_content.find(notices_start)
    ne_start = new_content.find(notices_end)
    if ns_idx >= 0 and ne_start >= 0 and ne_start >= ns_idx:
        between = new_content[ns_idx + len(notices_start) : ne_start].strip()
        if not between:
            ne_idx = ne_start + len(notices_end)
            if ns_idx > 0 and new_content[ns_idx - 1] == "\n":
                ns_idx -= 1
            if ne_idx < len(new_content) and new_content[ne_idx] == "\n":
                ne_idx += 1
            new_content = new_content[:ns_idx] + new_content[ne_idx:]

    new_content = new_content.rstrip("\n") + "\n" if new_content.strip() else ""

    try:
        agents_md.write_text(new_content, encoding="utf-8")
    except OSError:
        return False
    return True


def list_conflicts_sync() -> ConflictListOutput:
    """List unresolved conflicts across all sync roots."""

    conflicts: list[ConflictEntry] = []
    for sync_root in _find_sync_roots():
        conflicts.extend(
            conflict for conflict in _read_conflicts(sync_root) if not conflict.resolved
        )
    return ConflictListOutput(conflicts=conflicts)


def show_conflict_sync(conflict_id: str) -> ConflictShowOutput:
    """Show details for one conflict id."""

    _root, conflict = _find_conflict_by_id(conflict_id)
    if conflict is None:
        return ConflictShowOutput(error=f"Conflict '{conflict_id}' not found.")
    return ConflictShowOutput(conflict=conflict)


def resolve_conflict_sync(conflict_id: str) -> ConflictResolveOutput:
    """Mark a conflict as resolved and remove managed notices."""

    sync_root, conflict = _find_conflict_by_id(conflict_id)
    if conflict is None:
        # Idempotent: treat missing conflict as already resolved.
        return ConflictResolveOutput(conflict_id=conflict_id, resolved=True)

    if sync_root is None:
        return ConflictResolveOutput(
            conflict_id=conflict_id,
            resolved=False,
            error="Could not find sync root for conflict.",
        )

    notice_removed = _strip_agents_notice(sync_root, conflict_id)
    conflict_file = _conflict_dir(sync_root) / f"{conflict_id}.json"

    if conflict_file.exists():
        try:
            raw = json.loads(conflict_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Conflict metadata must be a JSON object.")
            raw["resolved"] = True
            raw["resolved_at"] = datetime.now(UTC).isoformat()

            temp_file = conflict_file.with_suffix(".json.tmp")
            temp_file.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            temp_file.replace(conflict_file)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return ConflictResolveOutput(
                conflict_id=conflict_id,
                resolved=False,
                notice_removed=notice_removed,
                error=str(exc),
            )

    return ConflictResolveOutput(
        conflict_id=conflict_id,
        resolved=True,
        notice_removed=notice_removed,
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
