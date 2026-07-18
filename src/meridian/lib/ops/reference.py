"""Shared session/spawn reference resolution helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.harness.session_detection import infer_harness_from_untracked_session_ref
from meridian.lib.ops.reference_recovery import (
    RecoveryResult,
    recover_harness_session_id,
)
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.state import primary_meta, session_identity, session_store, spawn_store
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.state.spawn.model import SpawnRecord

_SPAWN_REF_RE = re.compile(r"^p\d+$")
_CHAT_REF_RE = re.compile(r"^c\d+$")


@dataclass(frozen=True)
class ResolvedSessionReference:
    """Result of resolving a user-provided session/spawn reference."""

    harness_session_id: str | None
    harness: str | None
    source_chat_id: str | None
    source_model: str | None
    source_agent: str | None
    source_skills: tuple[str, ...]
    source_work_id: str | None
    tracked: bool
    source_control_root: str | None = None
    source_execution_cwd: str | None = None
    source_claude_config_dir: str | None = None
    source_pi_session_dir: str | None = None
    source_launch_policy_snapshot: LaunchPolicySnapshot | None = None
    warning: str | None = None
    recovery: RecoveryResult | None = None

    @property
    def missing_harness_session_id(self) -> bool:
        """True when a tracked reference exists but has no harness session id.

        Considers authoritative recovery (session_store, spawn_row, primary_meta)
        but excludes detected_unverified.
        """

        return self.tracked and self.authoritative_harness_session_id is None

    @property
    def effective_harness_session_id(self) -> str | None:
        """Return the harness session id, preferring recorded over recovered."""

        return self.harness_session_id or (
            self.recovery.harness_session_id if self.recovery is not None else None
        )

    @property
    def authoritative_harness_session_id(self) -> str | None:
        """Return the harness session id, using only authoritative recovery.

        Excludes DETECTED_UNVERIFIED — suitable for continue/fork paths
        that require verified session identity.
        """

        if self.harness_session_id:
            return self.harness_session_id
        if self.recovery is None:
            return None
        from meridian.lib.ops.reference_recovery import RecoveryProvenance

        if self.recovery.provenance == RecoveryProvenance.DETECTED_UNVERIFIED:
            return None
        return self.recovery.harness_session_id


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def resolve_spawn_ref(runtime_root: Path, ref: str) -> SpawnId | None:
    """Resolve a spawn reference from spawn id first, then chat id."""

    spawn = spawn_store.get_spawn(runtime_root, ref)
    if spawn is not None:
        return SpawnId(spawn.id)

    matches = session_identity.list_spawns_for_owner_chat(runtime_root, ref)
    if not matches:
        matches = spawn_store.list_spawns(runtime_root, filters={"chat_id": ref})
    if matches:
        matches.sort(key=lambda item: item.started_at or "", reverse=True)
        return SpawnId(matches[0].id)

    return None


def _latest_harness_session_id(record: session_store.SessionRecord) -> str | None:
    for candidate in reversed(record.harness_session_ids):
        normalized = candidate.strip()
        if normalized:
            return normalized
    return _normalize_optional(record.harness_session_id)


def _latest_primary_spawn_id_for_chat(runtime_root: Path, chat_id: str) -> str | None:
    rows = session_identity.list_spawns_for_owner_chat(runtime_root, chat_id)
    primary_rows = [row for row in rows if row.kind == "primary"]
    if not primary_rows:
        return None
    return primary_rows[-1].id


def _primary_launch_policy_snapshot(
    runtime_root: Path,
    *,
    spawn_row: SpawnRecord | None = None,
    chat_id: str | None = None,
) -> LaunchPolicySnapshot | None:
    """Load a persisted launch-policy snapshot for a primary session source."""

    if spawn_row is not None:
        return spawn_row.launch_policy_snapshot

    normalized_chat_id = (chat_id or "").strip()
    if not normalized_chat_id:
        return None

    primary_spawn_id = _latest_primary_spawn_id_for_chat(runtime_root, normalized_chat_id)
    if primary_spawn_id is None:
        return None

    primary_row = spawn_store.get_spawn(runtime_root, primary_spawn_id)
    if primary_row is None:
        return None
    return primary_row.launch_policy_snapshot


def _launch_policy_snapshot_for_session(
    runtime_root: Path,
    session: session_store.SessionRecord,
) -> LaunchPolicySnapshot | None:
    """Load a persisted launch-policy snapshot for a tracked session reference."""

    normalized_spawn_id = (session.spawn_id or "").strip()
    if session.kind == "spawn" or normalized_spawn_id:
        if not normalized_spawn_id:
            return None
        spawn_row = spawn_store.get_spawn(runtime_root, normalized_spawn_id)
        if spawn_row is None:
            return None
        return spawn_row.launch_policy_snapshot

    return _primary_launch_policy_snapshot(
        runtime_root,
        chat_id=session.chat_id,
    )


def _read_primary_pi_session_dir(runtime_root: Path, spawn_id: str) -> str | None:
    metadata = primary_meta.read_primary_metadata(runtime_root, spawn_id)
    if metadata is None:
        return None
    return _normalize_optional(metadata.session_dir)


def _resolve_untracked_reference(project_root: Path, ref: str) -> ResolvedSessionReference:
    registry = get_default_harness_registry()
    inferred_harness = infer_harness_from_untracked_session_ref(
        project_root,
        ref,
        registry=registry,
    )
    return ResolvedSessionReference(
        harness_session_id=ref,
        harness=str(inferred_harness) if inferred_harness is not None else None,
        source_chat_id=None,
        source_model=None,
        source_agent=None,
        source_skills=(),
        source_work_id=None,
        tracked=False,
        warning=(
            f"Session '{ref}' is not tracked yet; resuming with the provided harness session id."
        ),
    )


def _build_tracked_reference(
    *,
    harness_session_id: str | None,
    stored_harness: str | None,
    source_chat_id: str | None,
    source_model: str | None,
    source_agent: str | None,
    source_skills: tuple[str, ...],
    source_work_id: str | None,
    source_control_root: str | None = None,
    source_execution_cwd: str | None = None,
    source_claude_config_dir: str | None = None,
    source_pi_session_dir: str | None = None,
    source_launch_policy_snapshot: LaunchPolicySnapshot | None = None,
    project_root: Path,
) -> ResolvedSessionReference:
    registry = get_default_harness_registry()
    verified_harness = (
        infer_harness_from_untracked_session_ref(
            project_root,
            harness_session_id,
            registry=registry,
        )
        if harness_session_id is not None
        else None
    )
    return ResolvedSessionReference(
        harness_session_id=harness_session_id,
        harness=str(verified_harness) if verified_harness is not None else stored_harness,
        source_chat_id=source_chat_id,
        source_model=source_model,
        source_agent=source_agent,
        source_skills=source_skills,
        source_work_id=source_work_id,
        source_control_root=source_control_root,
        source_execution_cwd=source_execution_cwd,
        source_claude_config_dir=source_claude_config_dir,
        source_pi_session_dir=source_pi_session_dir,
        source_launch_policy_snapshot=source_launch_policy_snapshot,
        tracked=True,
    )


def _resolve_spawn_reference(
    runtime_root: Path, ref: str, project_root: Path
) -> ResolvedSessionReference:
    row = spawn_store.get_spawn(runtime_root, ref)
    if row is None:
        return _resolve_untracked_reference(project_root, ref)

    harness_session_id = _normalize_optional(row.harness_session_id)
    stored_harness = _normalize_optional(row.harness)
    source_execution_cwd = _normalize_optional(getattr(row, "task_cwd", None)) or row.execution_cwd
    source_control_root = (
        _normalize_optional(getattr(row, "control_root", None)) or project_root.as_posix()
    )
    if source_execution_cwd is None and row.harness == "claude" and row.kind == "child":
        # Legacy Claude child spawns executed from the spawn log directory.
        source_execution_cwd = resolve_spawn_log_dir(
            project_root, ref, runtime_root=runtime_root
        ).as_posix()
    elif source_execution_cwd is None:
        source_execution_cwd = project_root.as_posix()
    source_pi_session_dir: str | None = None
    if row.harness == "pi":
        if row.kind == "primary":
            source_pi_session_dir = _read_primary_pi_session_dir(runtime_root, row.id)
        elif session_identity.spawn_owner_chat_id(row):
            primary_spawn_id = _latest_primary_spawn_id_for_chat(
                runtime_root,
                session_identity.spawn_owner_chat_id(row) or "",
            )
            if primary_spawn_id is not None:
                source_pi_session_dir = _read_primary_pi_session_dir(runtime_root, primary_spawn_id)
    return _build_tracked_reference(
        harness_session_id=harness_session_id,
        stored_harness=stored_harness,
        source_chat_id=_normalize_optional(row.chat_id),
        source_model=_normalize_optional(row.model),
        source_agent=_normalize_optional(row.agent),
        source_skills=row.skills,
        source_work_id=_normalize_optional(row.work_id),
        source_control_root=source_control_root,
        source_execution_cwd=source_execution_cwd,
        source_claude_config_dir=_normalize_optional(row.claude_config_dir),
        source_pi_session_dir=source_pi_session_dir,
        source_launch_policy_snapshot=row.launch_policy_snapshot,
        project_root=project_root,
    )


def _resolve_chat_reference(
    runtime_root: Path, ref: str, project_root: Path
) -> ResolvedSessionReference:
    records = session_store.get_session_records(runtime_root, {ref})
    if not records:
        return _resolve_untracked_reference(project_root, ref)

    session = records[0]
    harness_session_id = _latest_harness_session_id(session)
    stored_harness = _normalize_optional(session.harness)
    source_pi_session_dir: str | None = None
    if stored_harness == "pi":
        owner_chat_id = session_identity.session_owner_chat_id(runtime_root, session)
        if owner_chat_id is not None:
            primary_spawn_id = _latest_primary_spawn_id_for_chat(runtime_root, owner_chat_id)
            if primary_spawn_id is not None:
                source_pi_session_dir = _read_primary_pi_session_dir(
                    runtime_root, primary_spawn_id
                )
    return _build_tracked_reference(
        harness_session_id=harness_session_id,
        stored_harness=stored_harness,
        source_chat_id=session.chat_id,
        source_model=_normalize_optional(session.model),
        source_agent=_normalize_optional(session.agent),
        source_skills=session.skills,
        source_work_id=_normalize_optional(session.active_work_id),
        source_control_root=(
            _normalize_optional(getattr(session, "control_root", None))
            or project_root.as_posix()
        ),
        source_execution_cwd=(
            _normalize_optional(getattr(session, "task_cwd", None))
            or session.execution_cwd
            or project_root.as_posix()
        ),
        source_claude_config_dir=_normalize_optional(session.claude_config_dir),
        source_pi_session_dir=source_pi_session_dir,
        source_launch_policy_snapshot=_launch_policy_snapshot_for_session(
            runtime_root,
            session,
        ),
        project_root=project_root,
    )


def _resolve_harness_session_reference(
    runtime_root: Path, ref: str, project_root: Path
) -> ResolvedSessionReference:
    session = session_store.resolve_session_ref(runtime_root, ref)
    if session is None:
        return _resolve_untracked_reference(project_root, ref)

    stored_harness_session_id = _normalize_optional(session.harness_session_id)
    harness_session_id = stored_harness_session_id or ref
    stored_harness = _normalize_optional(session.harness)
    source_pi_session_dir: str | None = None
    if stored_harness == "pi":
        owner_chat_id = session_identity.session_owner_chat_id(runtime_root, session)
        if owner_chat_id is not None:
            primary_spawn_id = _latest_primary_spawn_id_for_chat(runtime_root, owner_chat_id)
            if primary_spawn_id is not None:
                source_pi_session_dir = _read_primary_pi_session_dir(
                    runtime_root, primary_spawn_id
                )
    return _build_tracked_reference(
        harness_session_id=harness_session_id,
        stored_harness=stored_harness,
        source_chat_id=session.chat_id,
        source_model=_normalize_optional(session.model),
        source_agent=_normalize_optional(session.agent),
        source_skills=session.skills,
        source_work_id=_normalize_optional(session.active_work_id),
        source_control_root=(
            _normalize_optional(getattr(session, "control_root", None))
            or project_root.as_posix()
        ),
        source_execution_cwd=(
            _normalize_optional(getattr(session, "task_cwd", None))
            or session.execution_cwd
            or project_root.as_posix()
        ),
        source_claude_config_dir=_normalize_optional(session.claude_config_dir),
        source_pi_session_dir=source_pi_session_dir,
        source_launch_policy_snapshot=_launch_policy_snapshot_for_session(
            runtime_root,
            session,
        ),
        project_root=project_root,
    )


def _try_recover(
    project_root: Path,
    runtime_root: Path,
    ref: str,
    recorded_harness_session_id: str | None,
    recorded_harness: str | None,
) -> RecoveryResult | None:
    """Attempt recovery only when recorded ID is missing."""

    if recorded_harness_session_id and recorded_harness_session_id.strip():
        return None
    return recover_harness_session_id(
        project_root=project_root,
        runtime_root=runtime_root,
        ref=ref,
        recorded_harness_session_id=recorded_harness_session_id,
        recorded_harness=recorded_harness,
    )


def resolve_session_reference(
    project_root: Path,
    ref: str,
    *,
    runtime_root: Path | None = None,
) -> ResolvedSessionReference:
    """Resolve a session/spawn reference to harness session ID and source metadata."""

    normalized = ref.strip()
    if not normalized:
        raise ValueError("Session reference is required.")

    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        if not _SPAWN_REF_RE.fullmatch(normalized) and not _CHAT_REF_RE.fullmatch(normalized):
            return _resolve_untracked_reference(project_root, normalized)
        raise ValueError(f"Session reference '{normalized}' not found")
    if _SPAWN_REF_RE.fullmatch(normalized):
        result = _resolve_spawn_reference(resolved_runtime_root, normalized, project_root)
        if result.missing_harness_session_id:
            recovery = _try_recover(
                project_root=project_root,
                runtime_root=resolved_runtime_root,
                ref=normalized,
                recorded_harness_session_id=result.harness_session_id,
                recorded_harness=result.harness,
            )
            if recovery is not None:
                return replace(result, recovery=recovery)
        return result
    if _CHAT_REF_RE.fullmatch(normalized) or session_identity.is_tracked_chat_ref(
        resolved_runtime_root, normalized
    ):
        result = _resolve_chat_reference(resolved_runtime_root, normalized, project_root)
        if result.missing_harness_session_id:
            recovery = _try_recover(
                project_root=project_root,
                runtime_root=resolved_runtime_root,
                ref=normalized,
                recorded_harness_session_id=result.harness_session_id,
                recorded_harness=result.harness,
            )
            if recovery is not None:
                return replace(result, recovery=recovery)
        return result
    return _resolve_harness_session_reference(resolved_runtime_root, normalized, project_root)


__all__ = [
    "ResolvedSessionReference",
    "resolve_session_reference",
    "resolve_spawn_ref",
]
