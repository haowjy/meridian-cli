"""Shared session/spawn reference resolution helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from uuid import UUID

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
from meridian.lib.state.history_index import indexed_spawn_scan
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.state.session_authority import (
    NativeBindingStatus,
    NativeSessionKey,
    NativeSourceRef,
    OperationalBinding,
    PinnedSource,
    RecordedNativeSource,
    UnavailableBinding,
    UnobservedSource,
)
from meridian.lib.state.spawn.model import SpawnRecord

type NativePurpose = Literal["resume", "fork", "read", "context", "capture", "inspect"]
type NativeUnavailableReason = Literal[
    "unknown_ref",
    "reserved_or_reference_only",
    "legacy_unverified",
    "historical",
    "locator_unrecorded",
    "source_conflict",
    "authority_invalid",
    "durability_unresolved",
    "native_pending",
    "locator_unobserved",
    "unsupported_locator",
]
type SourceUseOperation = Literal["resume", "fork"]
type SourceUseRefusalReason = Literal[
    "unknown_ref", "tracked_run_unresolved", "native_claim_unavailable",
    "native_claim_ambiguous", "native_claim_blocked", "harness_mismatch",
]


@dataclass(frozen=True)
class AuthorizedNativeTarget:
    """Purpose-scoped journal authority; adapter preflight still owns live checks."""

    runtime_root: Path
    purpose: NativePurpose
    source: RecordedNativeSource


@dataclass(frozen=True)
class NativeInspection:
    """Non-authorizing metadata inspection, including legacy/unavailable status."""

    chat_id: str
    binding: NativeBindingStatus


@dataclass(frozen=True)
class NativeUnavailable:
    chat_id: str
    purpose: NativePurpose
    reason: NativeUnavailableReason
    key: NativeSessionKey | None = None


@dataclass(frozen=True)
class AuthorizedSourceUse:
    """Exact immutable source authorized for one resume/fork purpose."""

    operation: SourceUseOperation
    original_ref: str
    source: RecordedNativeSource
    source_run_id: str | None = None
    source_attempt_id: str | None = None
    source_boundary_event_id: str | None = None


@dataclass(frozen=True)
class UntrackedSourceUse:
    """A native ID with a complete strict negative recorded-claim lookup."""

    operation: SourceUseOperation
    original_ref: str
    native_id: str
    harness: str | None
    lookup_scope: Path


@dataclass(frozen=True)
class SourceUseRefused:
    """Tracked, ambiguous, unavailable, or otherwise non-authorizing input."""

    operation: SourceUseOperation
    original_ref: str
    reason: SourceUseRefusalReason
    canonical_chat_id: str | None = None


type SourceUseResult = AuthorizedSourceUse | UntrackedSourceUse | SourceUseRefused


async def resolve_native_reference(
    runtime_root: Path, chat_id: str, *, purpose: NativePurpose
) -> AuthorizedNativeTarget | NativeInspection | NativeUnavailable:
    """Resolve immutable cN authority for one use; never discover a native file.

    Native file availability and physical identity are deliberately outside
    this state/ops seam and must be checked by the owning harness adapter.
    """
    binding = session_store.get_native_binding(runtime_root, chat_id)
    if purpose == "inspect":
        return NativeInspection(chat_id, binding)
    if isinstance(binding, UnavailableBinding):
        return NativeUnavailable(chat_id, purpose, binding.reason, binding.key)

    source = _recorded_source_from_binding(binding)
    if source is None:
        if isinstance(binding.source, UnobservedSource):
            reason = (
                "unsupported_locator"
                if binding.source.first_observation.reason == "unsupported_locator"
                else "locator_unobserved"
            )
        else:
            reason = "native_pending"
        return NativeUnavailable(chat_id, purpose, reason, binding.key)

    return AuthorizedNativeTarget(runtime_root, purpose, source)


def _recorded_source_from_binding(binding: NativeBindingStatus) -> RecordedNativeSource | None:
    """Construct one immutable native source from an operational pin."""
    if not isinstance(binding, OperationalBinding) or not isinstance(binding.source, PinnedSource):
        return None
    source = binding.source
    return RecordedNativeSource(
        ref=NativeSourceRef(
            chat_id=binding.chat_id,
            binding_event_id=binding.binding_event_id,
            locator_event_id=source.locator_event_id,
        ),
        key=binding.key,
        locator=source.locator,
    )


def resolve_source_use(
    runtime_root: Path,
    operation: SourceUseOperation,
    original_ref: str,
    explicit_harness: str | None = None,
) -> SourceUseResult:
    """Normalize and authorize one resume/fork source without native discovery.

    State reads are strict and journal-backed. A failed, ambiguous, legacy, or
    contradictory lookup is never converted to an untracked result. ``--from``
    references and source-free launches are intentionally outside this API.
    """
    ref = original_ref.strip()
    harness = _normalize_optional(explicit_harness)
    harness = harness.lower() if harness is not None else None
    if not ref:
        return SourceUseRefused(operation, original_ref, "unknown_ref")

    if _CHAT_REF_RE.fullmatch(ref):
        authority = session_store.read_native_source_use_snapshot(runtime_root)
        if isinstance(authority, session_store.NativeIdUnavailable):
            return SourceUseRefused(operation, original_ref, "native_claim_unavailable")
        resolved = _native_source_for_use(authority, operation, original_ref, ref)
        if isinstance(resolved, AuthorizedSourceUse):
            if harness is not None and resolved.source.key.harness != harness:
                return SourceUseRefused(operation, original_ref, "harness_mismatch", ref)
            checked = _check_selected_native_claim(
                authority, operation, original_ref, resolved
            )
            if isinstance(checked, SourceUseRefused):
                return checked
        return resolved

    if _SPAWN_REF_RE.fullmatch(ref):
        row = spawn_store.get_spawn(runtime_root, ref)
        if row is None:
            return SourceUseRefused(operation, original_ref, "unknown_ref")
        row_harness = _normalize_optional(row.harness)
        row_harness = row_harness.lower() if row_harness is not None else None
        if harness is not None and row_harness is not None and harness != row_harness:
            return SourceUseRefused(operation, original_ref, "harness_mismatch")
        # pN is tracked run identity, not a native-session alias. Until an
        # authoritative terminal-attempt correlation exists, refuse it without
        # borrowing mutable row.chat_id or harness_session_id.
        return SourceUseRefused(operation, original_ref, "tracked_run_unresolved")

    authority = session_store.read_native_source_use_snapshot(runtime_root)
    if isinstance(authority, session_store.NativeIdUnavailable):
        return SourceUseRefused(operation, original_ref, "native_claim_unavailable")
    lookup = authority.candidates(ref)
    if isinstance(lookup, session_store.NativeIdNoMatch):
        return UntrackedSourceUse(operation, original_ref, ref, harness, runtime_root)
    if isinstance(lookup, session_store.NativeIdUnavailable):
        return SourceUseRefused(operation, original_ref, "native_claim_unavailable")
    candidates = tuple(
        item for item in lookup.candidates if harness is None or item.harness == harness
    )
    if not candidates:
        return SourceUseRefused(
            operation, original_ref, "harness_mismatch", lookup.candidates[0].chat_id
        )
    if len(candidates) != 1:
        return SourceUseRefused(operation, original_ref, "native_claim_ambiguous")
    candidate = candidates[0]
    resolved = _native_source_for_use(authority, operation, original_ref, candidate.chat_id)
    if not isinstance(resolved, AuthorizedSourceUse):
        return resolved
    checked = _check_selected_native_claim(authority, operation, original_ref, resolved)
    if isinstance(checked, SourceUseRefused):
        return checked
    key = resolved.source.key
    if key.native_session_id != ref:
        return SourceUseRefused(operation, original_ref, "native_claim_blocked", candidate.chat_id)
    if harness is not None and key.harness != harness:
        return SourceUseRefused(operation, original_ref, "harness_mismatch", candidate.chat_id)
    return resolved


def _native_source_for_use(
    authority: session_store.NativeSourceUseSnapshot,
    operation: SourceUseOperation,
    original_ref: str,
    chat_id: str,
) -> AuthorizedSourceUse | SourceUseRefused:
    binding = authority.binding(chat_id)
    if isinstance(binding, UnavailableBinding):
        return SourceUseRefused(operation, original_ref, "native_claim_blocked", chat_id)
    if not isinstance(binding.source, PinnedSource):
        return SourceUseRefused(operation, original_ref, "native_claim_blocked", chat_id)
    source = _recorded_source_from_binding(binding)
    if source is None:
        return SourceUseRefused(operation, original_ref, "native_claim_blocked", chat_id)
    return AuthorizedSourceUse(operation, original_ref, source)


def _check_selected_native_claim(
    authority: session_store.NativeSourceUseSnapshot,
    operation: SourceUseOperation,
    original_ref: str,
    resolved: AuthorizedSourceUse,
) -> SourceUseResult:
    key = resolved.source.key
    claims = authority.selected_chat_candidates(
        resolved.source.ref.chat_id, key.native_session_id, harness=key.harness
    )
    if isinstance(claims, session_store.NativeIdUnavailable):
        return SourceUseRefused(
            operation,
            original_ref,
            "native_claim_unavailable",
            resolved.source.ref.chat_id,
        )
    if isinstance(claims, session_store.NativeIdNoMatch):
        return SourceUseRefused(
            operation, original_ref, "native_claim_blocked", resolved.source.ref.chat_id
        )
    if isinstance(claims, session_store.NativeIdAmbiguous):
        return SourceUseRefused(
            operation, original_ref, "native_claim_ambiguous", resolved.source.ref.chat_id
        )
    candidate = claims.candidates[0]
    if (
        candidate.provenance != "v4"
        or candidate.protocol != "v4"
        or candidate.pin != "pinned"
        or candidate.blocked is not None
        or candidate.chat_id != resolved.source.ref.chat_id
        or candidate.store != key.store
        or candidate.harness != key.harness
        or candidate.native_session_id != key.native_session_id
    ):
        return SourceUseRefused(
            operation, original_ref, "native_claim_blocked", resolved.source.ref.chat_id
        )
    return resolved

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
    source_history_id: UUID | None = None
    source_spawn_id: str | None = None
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

    matches = list(indexed_spawn_scan(runtime_root, owner_chat_id=ref).records)
    if not matches:
        matches = list(indexed_spawn_scan(runtime_root, chat_id=ref).records)
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
    primary_rows = [row for row in rows.records if row.kind == "primary"]
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
    source_claude_config_dir: str | None = None,
    source_pi_session_dir: str | None = None,
    source_launch_policy_snapshot: LaunchPolicySnapshot | None = None,
    project_root: Path,
) -> ResolvedSessionReference:
    resolved_harness = stored_harness
    if resolved_harness is None and harness_session_id is not None:
        inferred = infer_harness_from_untracked_session_ref(
            project_root,
            harness_session_id,
        )
        resolved_harness = str(inferred) if inferred is not None else None
    return ResolvedSessionReference(
        harness_session_id=harness_session_id,
        harness=resolved_harness,
        source_chat_id=source_chat_id,
        source_model=source_model,
        source_agent=source_agent,
        source_skills=source_skills,
        source_work_id=source_work_id,
        source_history_id=source_history_id,
        source_spawn_id=source_spawn_id,
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
    if row.record_mode == "historical":
        raise ValueError("Historical sessions are inert; read or export the transcript instead.")

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
        source_history_id=row.history_id,
        source_spawn_id=row.id,
        source_control_root=source_control_root,
        source_execution_cwd=source_execution_cwd,
        source_claude_config_dir=_normalize_optional(row.claude_config_dir),
        source_pi_session_dir=source_pi_session_dir,
        source_launch_policy_snapshot=row.launch_policy_snapshot,
        project_root=project_root,
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
        linked = spawn_store.get_spawn(runtime_root, session.spawn_id)
        if (
            linked is not None
            and linked.chat_id == session.chat_id
            and (linked.session_instance_id in {None, session.session_instance_id})
        ):
            source_history_id = linked.history_id
    stored_harness = _normalize_optional(session.harness)
    source_pi_session_dir: str | None = None
    if stored_harness == "pi" and session.kind == "primary" and session.spawn_id:
        source_pi_session_dir = _read_primary_pi_session_dir(runtime_root, session.spawn_id)
    elif stored_harness == "pi":
        owner_chat_id = session_identity.session_owner_chat_id(runtime_root, session)
        if owner_chat_id is not None:
            primary_spawn_id = _latest_primary_spawn_id_for_chat(runtime_root, owner_chat_id)
            if primary_spawn_id is not None:
                source_pi_session_dir = _read_primary_pi_session_dir(runtime_root, primary_spawn_id)
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
        source_claude_config_dir=_normalize_optional(session.claude_config_dir),
        source_pi_session_dir=source_pi_session_dir,
        source_launch_policy_snapshot=_launch_policy_snapshot_for_session(
            runtime_root,
            session,
        ),
        project_root=project_root,
    )


def _resolve_chat_reference(
    runtime_root: Path, ref: str, project_root: Path
) -> ResolvedSessionReference:
    records = session_store.get_session_records(runtime_root, {ref})
    if not records:
        return _resolve_untracked_reference(project_root, ref)
    session = records[0]
    return _reference_from_session(
        runtime_root, session, project_root, _latest_harness_session_id(session)
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
    if resolved_runtime_root is None:
        if not _SPAWN_REF_RE.fullmatch(normalized) and not _CHAT_REF_RE.fullmatch(normalized):
            return _resolve_untracked_reference(project_root, normalized, harness_hint)
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
    return _resolve_harness_session_reference(
        resolved_runtime_root, normalized, project_root, harness_hint,
    )


def missing_fork_session_error(source_ref: str) -> str:
    """Return a consistent missing-session error for fork/continue flows."""
    if source_ref.startswith("p") and source_ref[1:].isdigit():
        return f"Spawn '{source_ref}' has no recorded session — cannot continue/fork."
    return f"Session '{source_ref}' has no recorded harness session — cannot continue/fork."


def missing_fork_session_error_with_discovery(
    *,
    source_ref: str,
    project_root: Path,
    source_harness: str | None,
    source_chat_id: str | None,
) -> str:
    """Add Pi primary-session discovery detail to a missing-session diagnostic."""
    normalized_ref = source_ref.strip()
    if (source_harness or "").strip().lower() != "pi":
        return missing_fork_session_error(normalized_ref)

    runtime_root = resolve_runtime_root_for_read(project_root)
    if runtime_root is None:
        return missing_fork_session_error(normalized_ref)

    spawn_id: str | None = normalized_ref if normalized_ref.startswith("p") else None
    if spawn_id is None:
        chat_ref = (
            normalized_ref
            if normalized_ref.startswith("c")
            else (source_chat_id or "").strip()
        )
        if chat_ref:
            owner_chat_id = session_identity.get_owner_chat_for_session(runtime_root, chat_ref)
            chat_spawns = session_identity.list_spawns_for_owner_chat(
                runtime_root, owner_chat_id or chat_ref
            )
            pi_primary_spawns = [
                row for row in chat_spawns.records if row.kind == "primary" and row.harness == "pi"
            ]
            if pi_primary_spawns:
                pi_primary_spawns.sort(key=lambda row: row.started_at or "", reverse=True)
                spawn_id = pi_primary_spawns[0].id

    if spawn_id is None:
        return missing_fork_session_error(normalized_ref)
    discovery, detail = primary_meta.read_primary_harness_session_discovery(runtime_root, spawn_id)
    if discovery == "never_created":
        return (
            f"Session '{normalized_ref}' has no Pi session — the original Pi session "
            "was ephemeral or never persisted a session."
        )
    if discovery == "discovery_failed":
        diagnostic = (
            detail.strip()
            if isinstance(detail, str) and detail.strip()
            else "no diagnostic detail"
        )
        return f"Session '{normalized_ref}' could not discover a Pi session — {diagnostic}."
    return missing_fork_session_error(normalized_ref)


__all__ = [
    "ResolvedSessionReference",
    "missing_fork_session_error",
    "missing_fork_session_error_with_discovery",
    "resolve_session_reference",
    "resolve_spawn_ref",
]
