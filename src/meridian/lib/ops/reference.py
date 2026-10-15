"""Shared session/spawn reference resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.core.refs import ParsedRef, parse_ref
from meridian.lib.core.types import RunId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.harness.session_detection import infer_harness_from_untracked_session_ref
from meridian.lib.ops.reference_recovery import (
    RecoveryResult,
    recover_harness_session_id,
)
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.state import session_identity, session_store, spawn_store
from meridian.lib.state.history_index import indexed_spawn_scan
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.runs import latest_bound_run, latest_run


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
    source_history_id: UUID | None = None
    source_spawn_id: str | None = None
    source_control_root: str | None = None
    source_execution_cwd: str | None = None
    source_native_store: str | None = None
    source_launch_policy_snapshot: LaunchPolicySnapshot | None = None
    warning: str | None = None
    recovery: RecoveryResult | None = None

    @property
    def missing_harness_session_id(self) -> bool:
        """True when a tracked reference lacks a complete recorded native key."""

        return self.tracked and self.authoritative_harness_session_id is None

    @property
    def effective_harness_session_id(self) -> str | None:
        """Return the harness session id, preferring recorded over recovered."""

        return self.harness_session_id or (
            self.recovery.harness_session_id if self.recovery is not None else None
        )

    @property
    def authoritative_harness_session_id(self) -> str | None:
        """A tracked ID is usable only as part of its complete recorded native key."""
        if self.tracked and not (
            _normalize_optional(self.harness)
            and _normalize_optional(self.source_native_store)
            and _normalize_optional(self.harness_session_id)
        ):
            return None
        return self.harness_session_id if self.tracked else self.effective_harness_session_id


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def resolve_spawn_ref(runtime_root: Path, ref: str) -> RunId | None:
    """Resolve a user reference to one exact local run row."""

    parsed = parse_ref(ref)
    if parsed.kind == "spawn":
        assert parsed.spawn_id is not None
        return latest_run(runtime_root, parsed.spawn_id)
    if parsed.kind == "run":
        assert parsed.run_id is not None
        return parsed.run_id if spawn_store.get_spawn(runtime_root, parsed.run_id) else None

    # Preserve exact access to nonconforming historical rows without teaching
    # another caller an ID predicate. This is not a bare spawn conversion.
    if (parsed.kind == "native" and Path(parsed.value).name == parsed.value
            and parsed.value not in {".", ".."}):
        row = spawn_store.get_spawn(runtime_root, parse_ref(parsed.value, row_key=True))
        if row is not None:
            return row.id

    matches = list(
        indexed_spawn_scan(runtime_root, owner_chat_id=parsed.value).records
    )
    if not matches:
        matches = list(
            indexed_spawn_scan(runtime_root, chat_id=parsed.value).records
        )
    if matches:
        matches.sort(key=lambda item: (item.started_at or "", item.id), reverse=True)
        return matches[0].id

    return None


def resolve_continue_source_row(
    runtime_root: Path,
    parsed: ParsedRef,
) -> SpawnRecord | None:
    """Select the exact row whose chat is the spawn's continue target."""

    if parsed.kind == "run":
        assert parsed.run_id is not None
        return spawn_store.get_spawn(runtime_root, parsed.run_id)
    if parsed.kind != "spawn":
        return None

    assert parsed.spawn_id is not None
    bound_run = latest_bound_run(runtime_root, parsed.spawn_id)
    if bound_run is None:
        latest = latest_run(runtime_root, parsed.spawn_id)
        if latest is None:
            return None
        if latest == parse_ref(str(parsed.spawn_id), row_key=True):
            return spawn_store.get_spawn(runtime_root, latest)
        raise ValueError(
            f"Spawn {parsed.spawn_id} never started a session; `--fork` is unavailable too."
        )
    return spawn_store.get_spawn(runtime_root, bound_run)


def _resolve_untracked_reference(
    project_root: Path, ref: str, harness_hint: str | None = None,
) -> ResolvedSessionReference:
    registry = get_default_harness_registry()
    inferred_harness = harness_hint or infer_harness_from_untracked_session_ref(
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
    source_history_id: UUID | None = None,
    source_spawn_id: str | None = None,
    source_control_root: str | None = None,
    source_execution_cwd: str | None = None,
    source_native_store: str | None = None,
    source_launch_policy_snapshot: LaunchPolicySnapshot | None = None,
) -> ResolvedSessionReference:
    return ResolvedSessionReference(
        harness_session_id=harness_session_id,
        harness=stored_harness,
        source_chat_id=source_chat_id,
        source_model=source_model,
        source_agent=source_agent,
        source_skills=source_skills,
        source_work_id=source_work_id,
        source_history_id=source_history_id,
        source_spawn_id=source_spawn_id,
        source_control_root=source_control_root,
        source_execution_cwd=source_execution_cwd,
        source_native_store=source_native_store,
        source_launch_policy_snapshot=source_launch_policy_snapshot,
        tracked=True,
    )


def _resolve_spawn_reference(
    runtime_root: Path, ref: str, project_root: Path
) -> ResolvedSessionReference:
    parsed = parse_ref(ref)
    row = resolve_continue_source_row(runtime_root, parsed)
    if row is None:
        raise ValueError(f"Spawn '{ref}' not found")
    if row.record_mode == "historical":
        raise ValueError("Historical sessions are inert; read or export the transcript instead.")

    # One selection, one resolution path: the continue target's session record
    # owns settings/work whether a hook, a verified exit or the entry chose it.
    target_chat_id = row.continue_chat_id
    target = session_store.get_session_record(runtime_root, target_chat_id) if (
        target_chat_id
    ) else None
    if target is not None:
        selected = _reference_from_session(
            runtime_root, target, project_root, target.harness_session_id,
        )
        return replace(selected, source_spawn_id=row.id)
    if target_chat_id != row.chat_id:
        # A missing selected chat never falls back to an older conversation.
        raise NativeSessionUnavailable(ref, "unbound")

    # Legacy rows with no authoritative session record resolve from the row.
    harness_session_id = _normalize_optional(row.harness_session_id)
    source_execution_cwd = _normalize_optional(getattr(row, "task_cwd", None)) or row.execution_cwd
    source_control_root = (
        _normalize_optional(getattr(row, "control_root", None)) or project_root.as_posix()
    )
    if source_execution_cwd is None and row.harness == "claude" and row.kind == "child":
        # Legacy Claude child spawns executed from the spawn log directory.
        source_execution_cwd = resolve_spawn_log_dir(
            project_root, row.id, runtime_root=runtime_root
        ).as_posix()
    elif source_execution_cwd is None:
        source_execution_cwd = project_root.as_posix()
    bound_session = session_identity.get_session_record_for_spawn(runtime_root, row.id)
    return _build_tracked_reference(
        harness_session_id=harness_session_id,
        stored_harness=_normalize_optional(row.harness),
        source_chat_id=_normalize_optional(row.chat_id),
        source_model=_normalize_optional(row.model),
        source_agent=_normalize_optional(row.agent),
        source_skills=row.skills,
        source_work_id=_normalize_optional(row.work_id),
        source_history_id=row.history_id,
        source_spawn_id=row.id,
        source_control_root=source_control_root,
        source_execution_cwd=source_execution_cwd,
        source_native_store=bound_session.native_store if bound_session else None,
        source_launch_policy_snapshot=row.launch_policy_snapshot,
    )


def _reference_from_session(
    runtime_root: Path,
    session: session_store.SessionRecord,
    project_root: Path,
    harness_session_id: str | None,
) -> ResolvedSessionReference:
    if session.record_mode == "historical":
        raise ValueError("Historical sessions are inert; read or export the transcript instead.")
    source_history_id = session.history_id
    if source_history_id is None and session.spawn_id:
        linked = spawn_store.get_spawn(
            runtime_root, parse_ref(session.spawn_id, row_key=True)
        )
        if (
            linked is not None
            and linked.chat_id == session.chat_id
            and (linked.session_instance_id in {None, session.session_instance_id})
        ):
            source_history_id = linked.history_id
    stored_harness = _normalize_optional(session.harness)
    return _build_tracked_reference(
        harness_session_id=harness_session_id,
        stored_harness=stored_harness,
        source_chat_id=session.chat_id,
        source_history_id=source_history_id,
        source_spawn_id=_normalize_optional(session.spawn_id),
        source_model=_normalize_optional(session.model),
        source_agent=_normalize_optional(session.agent),
        source_skills=session.skills,
        source_work_id=_normalize_optional(session.active_work_id),
        source_control_root=(
            _normalize_optional(getattr(session, "control_root", None)) or project_root.as_posix()
        ),
        source_execution_cwd=(
            _normalize_optional(getattr(session, "task_cwd", None))
            or session.execution_cwd
            or project_root.as_posix()
        ),
        source_native_store=session.native_store,
        source_launch_policy_snapshot=session_store.get_session_launch_policy_snapshot(
            runtime_root,
            session,
        ),
    )


def _resolve_chat_reference(
    runtime_root: Path, ref: str, project_root: Path
) -> ResolvedSessionReference:
    records = session_store.get_session_records(runtime_root, {ref})
    if not records:
        return _resolve_untracked_reference(project_root, ref)
    session = records[0]
    return _reference_from_session(
        runtime_root, session, project_root, _normalize_optional(session.harness_session_id)
    )


def _resolve_harness_session_reference(
    runtime_root: Path, ref: str, project_root: Path, harness_hint: str | None = None,
) -> ResolvedSessionReference:
    session = session_store.resolve_session_ref(runtime_root, ref, harness=harness_hint)
    if session is None:
        return _resolve_untracked_reference(project_root, ref, harness_hint)
    if harness_hint is None:
        inferred = infer_harness_from_untracked_session_ref(project_root, ref)
        if inferred is not None and session.harness and str(inferred) != session.harness:
            raise ValueError(
                "Native session reference is ambiguous across harnesses; specify --harness."
            )
    return _reference_from_session(runtime_root, session, project_root, ref)


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
    harness_hint: str | None = None,
) -> ResolvedSessionReference:
    """Resolve a session/spawn reference to harness session ID and source metadata."""

    normalized = ref.strip()
    harness_hint = _normalize_optional(harness_hint)
    if not normalized:
        raise ValueError("Session reference is required.")

    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    parsed = parse_ref(normalized)
    if resolved_runtime_root is None:
        if parsed.kind not in {"spawn", "run", "chat"}:
            return _resolve_untracked_reference(project_root, normalized, harness_hint)
        raise ValueError(f"Session reference '{normalized}' not found")
    if parsed.kind in {"spawn", "run"}:
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
    if parsed.kind == "chat" or session_identity.is_tracked_chat_ref(
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
    return _resolve_harness_session_reference(
        resolved_runtime_root, normalized, project_root, harness_hint,
    )


def validate_continue_reference(ref: str) -> None:
    """Exact continues name a spawn or a segment, never an immutable run."""
    parsed = parse_ref(ref)
    if parsed.kind == "run":
        raise ValueError(
            f"Cannot continue run '{parsed.value}'; continue the spawn "
            f"(`{parsed.spawn_id}`) or fork the run (`--fork {parsed.value}`)."
        )


__all__ = [
    "ResolvedSessionReference",
    "resolve_continue_source_row",
    "resolve_session_reference",
    "resolve_spawn_ref",
    "validate_continue_reference",
]
