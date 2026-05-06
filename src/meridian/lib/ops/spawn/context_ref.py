"""Resolve and render prior context references for spawn prompts."""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.spawn.model import SpawnRecord

from .query import (
    read_report_text,
    read_spawn_row,
    read_written_files,
    resolve_spawn_reference,
)

_SESSION_REF_RE = re.compile(r"^c\d+$")
_SPAWN_REF_RE = re.compile(r"^p\d+$")


class SpawnContextRef(BaseModel):
    """Resolved context payload sourced from one prior spawn."""

    model_config = ConfigDict(frozen=True)

    ref_kind: Literal["spawn"] = "spawn"
    spawn_id: str
    status: str
    agent: str
    desc: str
    model: str
    harness: str
    report_text: str | None = None
    written_files: tuple[str, ...] = ()
    harness_session_id: str | None = None
    chat_id: str | None = None


class SessionContextRef(BaseModel):
    """Resolved context payload sourced from one prior chat/session."""

    model_config = ConfigDict(frozen=True)

    ref_kind: Literal["session"] = "session"
    chat_id: str
    primary_spawn_id: str
    status: str
    agent: str
    model: str
    harness: str
    harness_session_id: str | None = None


type ContextRef = SpawnContextRef | SessionContextRef


def _select_primary_spawn_for_session(project_root: Path, chat_id: str) -> SpawnRecord:
    from meridian.lib.state.reaper import reconcile_spawns

    runtime_root = resolve_runtime_root_for_read(project_root)
    spawns = reconcile_spawns(
        runtime_root,
        spawn_store.list_spawns(runtime_root, filters={"chat_id": chat_id}),
    )
    primary_spawns = [row for row in spawns if row.kind == "primary"]
    if not primary_spawns:
        raise ValueError(f"No primary spawn found for session '{chat_id}'")

    return primary_spawns[-1]


def _is_tracked_session(project_root: Path, chat_id: str) -> bool:
    runtime_root = resolve_runtime_root_for_read(project_root)
    return bool(session_store.get_session_records(runtime_root, {chat_id}))


def _load_report_text(project_root: Path, spawn_id: str) -> str | None:
    _, report_text = read_report_text(project_root, spawn_id)
    return report_text


def _load_written_files(project_root: Path, spawn_id: str) -> tuple[str, ...]:
    try:
        return read_written_files(project_root, spawn_id)
    except (FileNotFoundError, OSError):
        return ()


def resolve_context_ref(project_root: Path, ref: str) -> ContextRef:
    """Resolve one --from value to concrete prior context payload."""

    normalized = ref.strip()
    if not normalized:
        raise ValueError("context reference is required")

    if normalized.startswith("@") or _SPAWN_REF_RE.fullmatch(normalized):
        spawn_id = resolve_spawn_reference(project_root, normalized)
        spawn_row = read_spawn_row(project_root, spawn_id)
        if spawn_row is None:
            raise ValueError(f"Spawn '{spawn_id}' not found")
        return _spawn_context_ref(spawn_row, project_root)
    if _SESSION_REF_RE.fullmatch(normalized) or _is_tracked_session(project_root, normalized):
        primary_row = _select_primary_spawn_for_session(project_root, normalized)
        return _session_context_ref(primary_row)

    spawn_id = resolve_spawn_reference(project_root, normalized)
    row = read_spawn_row(project_root, spawn_id)
    if row is None:
        raise ValueError(f"Spawn '{spawn_id}' not found")
    return _spawn_context_ref(row, project_root)


def _spawn_context_ref(row: SpawnRecord, project_root: Path) -> SpawnContextRef:
    return SpawnContextRef(
        spawn_id=row.id,
        status=row.status,
        agent=row.agent or "",
        desc=row.desc or "",
        model=row.model or "",
        harness=row.harness or "",
        report_text=_load_report_text(project_root, row.id),
        written_files=_load_written_files(project_root, row.id),
        harness_session_id=row.harness_session_id,
        chat_id=row.chat_id,
    )


def _session_context_ref(primary_row: SpawnRecord) -> SessionContextRef:
    chat_id = (primary_row.chat_id or "").strip()
    if not chat_id:
        raise ValueError(f"Primary spawn '{primary_row.id}' has no associated session")
    return SessionContextRef(
        chat_id=chat_id,
        primary_spawn_id=primary_row.id,
        status=primary_row.status,
        agent=primary_row.agent or "",
        model=primary_row.model or "",
        harness=primary_row.harness or "",
        harness_session_id=primary_row.harness_session_id,
    )


def resolved_context_ref_value(ref: ContextRef) -> str:
    """Return the external resolved reference value for operation output."""

    if ref.ref_kind == "session":
        return ref.chat_id
    return ref.spawn_id


def _render_context_ref(ref: ContextRef) -> str:
    status = ref.status or "unknown"
    agent = ref.agent or "n/a"

    if ref.ref_kind == "session":
        tag_attrs = f'chat="{ref.chat_id}" primary_spawn="{ref.primary_spawn_id}"'
        transcript_ref = (
            ref.chat_id if _SESSION_REF_RE.fullmatch(ref.chat_id) else ref.primary_spawn_id
        )
        lines = [
            f"<prior-session-context {tag_attrs}>",
            f"# Prior session: {ref.chat_id}",
            f"**Primary spawn:** {ref.primary_spawn_id} | "
            f"**Status:** {status} | **Agent:** {agent}",
            "",
            "## Explore Further",
            f"- Session transcript: `meridian session log {transcript_ref}`",
            f"- Primary spawn: `meridian spawn show {ref.primary_spawn_id}`",
        ]
        if ref.harness_session_id and ref.harness_session_id.strip():
            lines.insert(
                3,
                f"**Harness:** {ref.harness or 'n/a'} | "
                f"**Harness session:** {ref.harness_session_id}",
            )
        lines.append("</prior-session-context>")
        return "\n".join(lines)

    desc = ref.desc or "n/a"
    lines = [
        f'<prior-spawn-context spawn="{ref.spawn_id}">',
        f"# Prior spawn: {ref.spawn_id}",
        f"**Status:** {status} | **Agent:** {agent} | **Desc:** {desc}",
        "",
        "## Report",
    ]
    if ref.report_text and ref.report_text.strip():
        lines.append(ref.report_text.strip())
    else:
        lines.append("No report available.")

    if ref.written_files:
        lines.append("")
        lines.append("## Files Modified")
        lines.extend(f"- {path}" for path in ref.written_files)

    lines.append("")
    lines.append("## Explore Further")
    lines.append(f"- Full details: `meridian spawn show {ref.spawn_id}`")
    lines.append(f"- Read modified files: `meridian spawn files {ref.spawn_id}`")
    if ref.chat_id and ref.chat_id.strip():
        lines.append(f"- Session transcript: `meridian session log {ref.chat_id}`")
    lines.append("</prior-spawn-context>")
    return "\n".join(lines)


def render_context_refs(refs: Sequence[ContextRef]) -> str:
    """Render resolved --from references as prior-context prompt blocks."""

    if not refs:
        return ""
    return "\n\n".join(_render_context_ref(ref) for ref in refs)


__all__ = [
    "ContextRef",
    "SessionContextRef",
    "SpawnContextRef",
    "render_context_refs",
    "resolve_context_ref",
    "resolved_context_ref_value",
]
