"""Shared session/spawn reference resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.harness.session_detection import infer_harness_from_untracked_session_ref
from meridian.lib.ops.locate import SpawnHit, Untracked, chat_spawn, locate
from meridian.lib.ops.reference_recovery import (
    RecoveryResult,
    recover_harness_session_id,
)
from meridian.lib.state import session_identity, session_store, spawn_store
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn.model import SpawnRecord


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
    source_goal: str | None = None
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


def _resolve_untracked_reference(
    project_root: Path,
    ref: str,
    harness_hint: str | None = None,
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
    source_goal: str | None = None,
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
        source_goal=source_goal,
        tracked=True,
    )


def _resolve_spawn_reference(
    runtime_root: Path, row: SpawnRecord, project_root: Path
) -> ResolvedSessionReference:
    ref = row.id
    if row.record_mode == "historical":
        raise ValueError("Historical sessions are inert; read or export the transcript instead.")

    # One selection, one resolution path: the continue target's session record
    # owns settings/work whether a hook, a verified exit or the entry chose it.
    target_chat_id = row.continue_chat_id
    target = session_store.chat_record(runtime_root, target_chat_id) if (target_chat_id) else None
    if target is not None:
        selected = _reference_from_session(
            runtime_root,
            target,
            project_root,
            target.harness_session_id,
            spawn_id=target.spawn_id,
        )
        return replace(selected, source_spawn_id=row.id, source_goal=row.goal)
    if target_chat_id is None and (row.has_no_session_of_its_own):
        raise ValueError(
            f"{ref} never started a session; nothing to continue, use --fork or a new spawn"
        )
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
        source_execution_cwd = LaunchPaths.for_row(runtime_root, row).dir.as_posix()
    elif source_execution_cwd is None:
        source_execution_cwd = project_root.as_posix()
    bound_session = session_identity.get_session_record_for_spawn(runtime_root, row.id)
    return _build_tracked_reference(
        harness_session_id=harness_session_id,
        stored_harness=_normalize_optional(row.harness),
        source_chat_id=_normalize_optional(row.entry_chat_id),
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
        source_goal=row.goal,
    )


def _reference_from_session(
    runtime_root: Path,
    session: session_store.SessionRecord,
    project_root: Path,
    harness_session_id: str | None,
    *,
    spawn_id: str | None,
) -> ResolvedSessionReference:
    if session.record_mode == "historical":
        raise ValueError("Historical sessions are inert; read or export the transcript instead.")
    linked = spawn_store.get_spawn(runtime_root, spawn_id) if spawn_id is not None else None
    source_history_id = session.history_id
    if (
        source_history_id is None
        and linked is not None
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
        source_spawn_id=_normalize_optional(spawn_id),
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
        source_goal=linked.goal if linked is not None else None,
    )


def resolve_session_reference(
    project_root: Path,
    ref: str,
    *,
    runtime_root: Path | None = None,
    harness_hint: str | None = None,
) -> ResolvedSessionReference:
    """Resolve a reference to the session to resume, with its source metadata.

    A spawn ref (or selector) resumes that spawn's continue chat; a chat ref or native
    ID resumes that chat. A native ID with no record resumes untracked.
    """

    harness_hint = _normalize_optional(harness_hint)
    located = locate(project_root, ref, runtime_root=runtime_root, harness=harness_hint)
    if isinstance(located, Untracked):
        return _resolve_untracked_reference(project_root, located.native_id, harness_hint)

    root = located.runtime_root
    if isinstance(located, SpawnHit):
        result = _resolve_spawn_reference(root, located.spawn, project_root)
        if (
            result.missing_harness_session_id
            and _normalize_optional(result.harness_session_id) is None
        ):
            recovery = recover_harness_session_id(root, located.spawn)
            if recovery is not None:
                return replace(result, recovery=recovery)
        return result
    chat = located.chat
    if located.ref.kind == "native" and harness_hint is None:
        inferred = infer_harness_from_untracked_session_ref(project_root, located.ref.value)
        if inferred is not None and chat.harness and str(inferred) != chat.harness:
            raise ValueError(
                "Native session reference is ambiguous across harnesses; specify --harness."
            )
    owner = chat_spawn(located)
    return _reference_from_session(
        root,
        chat,
        project_root,
        _normalize_optional(chat.harness_session_id),
        spawn_id=str(owner) if owner is not None else None,
    )


__all__ = [
    "ResolvedSessionReference",
    "resolve_session_reference",
]
