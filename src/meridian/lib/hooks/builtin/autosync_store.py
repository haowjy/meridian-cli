"""Autosync artifact storage — conflict metadata, sync state, AGENTS.md notices.

Single owner of the .meridian/autosync/ file layout. All reads and writes
go through this module. No other module should construct paths into
.meridian/autosync/ or parse its JSON directly.

Dependencies: stdlib plus plugin_api only, preserving standalone extraction by
keeping Meridian internals behind the public plugin boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from meridian.plugin_api.fs import AtomicReplaceDurabilityError, atomic_write_text

AUTOSYNC_IGNORE_PATTERNS: tuple[str, ...] = (".git", "**/.git", ".meridian/autosync/")

_NOTICE_START = "<!-- autosync-notices -->"
_NOTICE_END = "<!-- /autosync-notices -->"


@dataclass(frozen=True)
class ConflictRecord:
    """One conflict, as stored on disk."""

    id: str
    context: str
    sync_root: str
    conflict_type: str
    paths: tuple[str, ...]
    local_sha: str
    remote_sha: str
    remote_branch: str
    event_name: str
    spawn_id: str | None
    created_at: str
    resolved: bool
    resolved_at: str | None = None


@dataclass(frozen=True)
class SyncState:
    """Last sync outcome for a sync root."""

    last_sync: str
    outcome: str
    conflict_id: str | None = None


@dataclass(frozen=True)
class SyncRootStatus:
    """Aggregated status for one sync root."""

    state: SyncState | None
    unresolved_conflicts: tuple[ConflictRecord, ...]


def has_autosync_state(sync_root: Path) -> bool:
    """Return whether autosync artifacts exist for the sync root."""

    return conflict_dir(sync_root).exists() or state_file(sync_root).exists()


def conflict_dir(sync_root: Path) -> Path:
    """Return the conflict metadata directory for a sync root."""

    return sync_root / ".meridian" / "autosync" / "conflicts"


def state_file(sync_root: Path) -> Path:
    """Return the sync state file path for a sync root."""

    return sync_root / ".meridian" / "autosync" / "state.json"


def read_sync_state(sync_root: Path) -> SyncState | None:
    """Read sync state for one root."""

    target = state_file(sync_root)
    if not target.exists():
        return None
    data = _load_json_object(target)
    if data is None:
        return None

    last_sync_raw = data.get("last_sync")
    last_sync = last_sync_raw if isinstance(last_sync_raw, str) and last_sync_raw.strip() else ""

    return SyncState(
        last_sync=last_sync,
        outcome=str(data.get("outcome", "unknown")),
        conflict_id=_to_optional_str(data.get("conflict_id")),
    )


def read_conflicts(sync_root: Path) -> list[ConflictRecord]:
    """Read all conflict records for one sync root."""

    root = conflict_dir(sync_root)
    if not root.exists():
        return []

    try:
        files = sorted(root.iterdir())
    except OSError:
        return []

    records: list[ConflictRecord] = []
    for file_path in files:
        if file_path.suffix != ".json":
            continue
        data = _load_json_object(file_path)
        if data is None:
            continue

        conflict_id_raw = data.get("id")
        conflict_id = (
            conflict_id_raw
            if isinstance(conflict_id_raw, str) and conflict_id_raw
            else file_path.stem
        )

        trigger_obj = _as_object_dict(data.get("trigger")) or {}
        event_name = str(trigger_obj.get("event", "unknown"))
        spawn_id = _to_optional_str(trigger_obj.get("spawn_id"))

        paths = _to_string_tuple(data.get("paths"))

        records.append(
            ConflictRecord(
                id=conflict_id,
                context=str(data.get("context", "unknown")),
                sync_root=str(data.get("sync_root", sync_root.as_posix())),
                conflict_type=str(data.get("conflict_type", "unknown")),
                paths=paths,
                local_sha=str(data.get("local_sha", "unknown")),
                remote_sha=str(data.get("remote_sha", "unknown")),
                remote_branch=str(data.get("remote_branch", "main")),
                event_name=event_name,
                spawn_id=spawn_id,
                created_at=str(data.get("created_at", "unknown")),
                resolved=bool(data.get("resolved", False)),
                resolved_at=_to_optional_str(data.get("resolved_at")),
            )
        )

    return records


def read_unresolved_conflicts(sync_root: Path) -> list[ConflictRecord]:
    """Read unresolved conflict records for one sync root."""

    return [record for record in read_conflicts(sync_root) if not record.resolved]


def find_conflict_by_id(
    sync_roots: list[Path],
    conflict_id: str,
) -> tuple[Path | None, ConflictRecord | None]:
    """Find one conflict ID across provided sync roots."""

    for sync_root in sync_roots:
        for record in read_conflicts(sync_root):
            if record.id == conflict_id:
                return sync_root, record
    return None, None


def read_status(sync_root: Path) -> SyncRootStatus:
    """Read aggregate autosync status for one sync root."""

    unresolved = tuple(read_unresolved_conflicts(sync_root))
    return SyncRootStatus(state=read_sync_state(sync_root), unresolved_conflicts=unresolved)


def has_unresolved_conflict(sync_root: Path) -> bool:
    """Return whether any unresolved conflict exists for this root."""

    return any(not record.resolved for record in read_conflicts(sync_root))


def generate_conflict_id() -> str:
    """Generate a conflict ID like c20260512-001."""

    date_str = datetime.now(UTC).strftime("%Y%m%d")
    short_hash = hashlib.sha256(str(time.monotonic_ns()).encode("utf-8")).hexdigest()[:3]
    return f"c{date_str}-{short_hash}"


def write_conflict(sync_root: Path, record: ConflictRecord) -> None:
    """Write one conflict metadata JSON atomically."""

    target_dir = conflict_dir(sync_root)
    target_dir.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "id": record.id,
        "context": record.context,
        "sync_root": record.sync_root,
        "conflict_type": record.conflict_type,
        "paths": list(record.paths),
        "local_sha": record.local_sha,
        "remote_sha": record.remote_sha,
        "remote_branch": record.remote_branch,
        "trigger": {
            "event": record.event_name,
            "spawn_id": record.spawn_id,
        },
        "created_at": record.created_at,
        "resolved": record.resolved,
    }
    if record.resolved_at is not None:
        payload["resolved_at"] = record.resolved_at

    target = target_dir / f"{record.id}.json"
    atomic_write_text(target, json.dumps(payload, indent=2))


def write_sync_state(
    sync_root: Path,
    *,
    outcome: str,
    conflict_id: str | None = None,
) -> None:
    """Write autosync state JSON atomically."""

    target_dir = sync_root / ".meridian" / "autosync"
    target_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "last_sync": datetime.now(UTC).isoformat(),
        "outcome": outcome,
        "conflict_id": conflict_id,
    }
    atomic_write_text(state_file(sync_root), json.dumps(payload, indent=2))


def mark_resolved(sync_root: Path, conflict_id: str) -> bool:
    """Mark one conflict as resolved. Returns True when metadata was updated."""

    try:
        target, data = _find_conflict_file(sync_root, conflict_id)
    except OSError:
        return False
    if target is None or data is None:
        return False

    data["resolved"] = True
    data["resolved_at"] = datetime.now(UTC).isoformat()
    content = json.dumps(data, indent=2)
    try:
        return _write_text_reconciled(target, content)
    except OSError:
        return False


def append_conflict_notice(
    sync_root: Path,
    conflict_id: str,
    paths: list[str],
    remote_branch: str,
) -> bool:
    """Append a managed conflict notice to AGENTS.md."""

    agents_md = sync_root / "AGENTS.md"
    if not agents_md.exists():
        return False

    try:
        content = agents_md.read_text(encoding="utf-8")
    except OSError:
        return False

    conflict_start = _conflict_block_start(conflict_id)
    conflict_end = _conflict_block_end(conflict_id)
    conflict_pattern = re.compile(
        rf"{re.escape(conflict_start)}.*?{re.escape(conflict_end)}",
        flags=re.DOTALL,
    )
    if conflict_pattern.search(content):
        return False

    notice_block = _format_conflict_notice(conflict_id, paths, remote_branch)

    if _NOTICE_START in content and _NOTICE_END in content:
        start_idx = content.index(_NOTICE_START)
        end_idx = content.index(_NOTICE_END)
        if start_idx >= end_idx:
            return False

        prefix = content[:end_idx]
        if not prefix.endswith("\n"):
            prefix += "\n"
        new_content = prefix + notice_block + "\n" + content[end_idx:]
    elif _NOTICE_START in content or _NOTICE_END in content:
        return False
    else:
        section = f"\n{_NOTICE_START}\n{notice_block}\n{_NOTICE_END}\n"
        new_content = content.rstrip("\n") + "\n" + section

    try:
        return _write_text_reconciled(agents_md, new_content)
    except OSError:
        return False


def strip_conflict_notice(sync_root: Path, conflict_id: str) -> bool:
    """Remove one managed conflict notice block from AGENTS.md."""

    agents_md = sync_root / "AGENTS.md"
    if not agents_md.exists():
        return False

    try:
        content = agents_md.read_text(encoding="utf-8")
    except OSError:
        return False

    start_marker = _conflict_block_start(conflict_id)
    end_marker = _conflict_block_end(conflict_id)

    start_idx = content.find(start_marker)
    end_start_idx = content.find(end_marker)
    if start_idx < 0 or end_start_idx < 0 or end_start_idx < start_idx:
        return False

    end_idx = end_start_idx + len(end_marker)
    if end_idx < len(content) and content[end_idx] == "\n":
        end_idx += 1

    new_content = content[:start_idx] + content[end_idx:]

    ns_idx = new_content.find(_NOTICE_START)
    ne_start = new_content.find(_NOTICE_END)
    if ns_idx >= 0 and ne_start >= 0 and ne_start >= ns_idx:
        between = new_content[ns_idx + len(_NOTICE_START) : ne_start].strip()
        if not between:
            ne_idx = ne_start + len(_NOTICE_END)
            if ns_idx > 0 and new_content[ns_idx - 1] == "\n":
                ns_idx -= 1
            if ne_idx < len(new_content) and new_content[ne_idx] == "\n":
                ne_idx += 1
            new_content = new_content[:ns_idx] + new_content[ne_idx:]

    new_content = new_content.rstrip("\n") + "\n" if new_content.strip() else ""
    try:
        return _write_text_reconciled(agents_md, new_content)
    except OSError:
        return False


def _write_text_reconciled(path: Path, content: str) -> bool:
    """Write content, verifying visibility after a post-commit durability error."""

    try:
        atomic_write_text(path, content)
    except AtomicReplaceDurabilityError:
        try:
            return path.read_text(encoding="utf-8") == content
        except OSError:
            return False
    return True


def _conflict_block_start(conflict_id: str) -> str:
    return f"<!-- autosync-conflict:{conflict_id} -->"


def _conflict_block_end(conflict_id: str) -> str:
    return f"<!-- /autosync-conflict:{conflict_id} -->"


def _format_conflict_notice(conflict_id: str, paths: list[str], remote_branch: str) -> str:
    paths_str = ", ".join(f"`{path}`" for path in paths)
    return (
        f"{_conflict_block_start(conflict_id)}\n"
        "### Autosync Conflict\n"
        "\n"
        f"Conflict `{conflict_id}` — autosync could not merge remote changes.\n"
        f"Affected paths: {paths_str}. The working tree retains the\n"
        f"local version. Remote changes are at `origin/{remote_branch}`.\n"
        "\n"
        "To resolve:\n"
        "1. `git fetch origin`\n"
        f"2. `git merge origin/{remote_branch}`\n"
        "3. Resolve any conflicts in the affected files\n"
        "4. `git add` the resolved files and `git commit`\n"
        "5. Remove this notice block when done.\n"
        "\n"
        "If you are not modifying the affected files, this does not block your work.\n"
        f"{_conflict_block_end(conflict_id)}"
    )


def _load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    typed_raw = cast("dict[object, object]", raw)
    return {str(key): value for key, value in typed_raw.items()}


def _as_object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    typed = cast("dict[object, object]", value)
    return {str(key): item for key, item in typed.items()}


def _to_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    typed = cast("list[object]", value)
    return tuple(str(item) for item in typed)


def _to_optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _find_conflict_file(
    sync_root: Path,
    conflict_id: str,
) -> tuple[Path | None, dict[str, Any] | None]:
    root = conflict_dir(sync_root)
    direct = root / f"{conflict_id}.json"
    direct_data = _load_json_object(direct) if direct.exists() else None
    if direct_data is not None:
        return direct, direct_data

    for file_path in root.glob("*.json"):
        data = _load_json_object(file_path)
        if data is None:
            continue
        if str(data.get("id", file_path.stem)) == conflict_id:
            return file_path, data
    return None, None


__all__ = [
    "AUTOSYNC_IGNORE_PATTERNS",
    "ConflictRecord",
    "SyncRootStatus",
    "SyncState",
    "append_conflict_notice",
    "conflict_dir",
    "find_conflict_by_id",
    "generate_conflict_id",
    "has_autosync_state",
    "has_unresolved_conflict",
    "mark_resolved",
    "read_conflicts",
    "read_status",
    "read_sync_state",
    "read_unresolved_conflicts",
    "state_file",
    "strip_conflict_notice",
    "write_conflict",
    "write_sync_state",
]
