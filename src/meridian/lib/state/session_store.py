"""File-backed session tracking for a Meridian state root's `sessions.jsonl`."""

import json
import os
import re
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any, Literal, NamedTuple, cast

import psutil
from pydantic import BaseModel, ValidationError

from meridian.lib.core.types import (
    ChatId,
    HarnessSessionId,
    normalize_optional_identity,
)
from meridian.lib.platform.atomic import fsync_directory
from meridian.lib.platform.locking import (
    acquire_file_lock,
    lock_file,
    release_file_lock,
    try_lock_file,
    unlink_validated_lock,
)
from meridian.lib.state.atomic import append_text_line, atomic_write_bytes, atomic_write_text
from meridian.lib.state.event_store import read_events, utc_now_iso
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.paths import RuntimePaths, normalize_path_for_write
from meridian.lib.state.session_authority import (
    AcceptedBoundary,
    AcquiredStoreGuard,
    AttemptFact,
    AttemptResult,
    BeginIntent,
    BeginIntentV4,
    BoundaryFact,
    BoundaryFactV4,
    BoundModelIntent,
    FactsUnavailable,
    Historical,
    IdentityDelta,
    InvalidSessionJournal,
    JournalRead,
    JournalSnapshot,
    LocatorConflictEvent,
    LocatorUnrecorded,
    NativeBindingStatus,
    NeedChat,
    NoOp,
    OperationalBinding,
    Pending,
    PendingSource,
    Pinned,
    PinnedSource,
    RecordedNativeSource,
    ReferenceOnly,
    Refutation,
    RefutationV4,
    ReplayModelFacts,
    UnavailableBinding,
    Unobserved,
    UnobservedSource,
    _generation_matches,
    canonical_chat_number,
    eligible_input_entry,
    native_key_tuple,
    plan_attempt,
    plan_identity,
    read_journal,
    requested_source_eligible,
    result_for_boundary,
    selection_startup_key,
    startup_key,
)
from meridian.lib.state.session_authority import (
    AttemptBoundaries as AttemptBoundaries,
)
from meridian.lib.state.session_authority import (
    ConversationModelSelection as ConversationModelSelection,
)
from meridian.lib.state.session_authority import (
    NativeSessionKey as NativeSessionKey,
)
from meridian.lib.state.session_authority import (
    SessionAttemptEvent as SessionAttemptEvent,
)
from meridian.lib.state.session_authority import (
    SessionEvent as SessionEvent,
)
from meridian.lib.state.session_authority import (
    SessionHistoricalEvent as SessionHistoricalEvent,
)
from meridian.lib.state.session_authority import (
    SessionModelObservationEvent as SessionModelObservationEvent,
)
from meridian.lib.state.session_authority import (
    SessionModelSelectionEvent as SessionModelSelectionEvent,
)
from meridian.lib.state.session_authority import (
    SessionRecord as SessionRecord,
)
from meridian.lib.state.session_authority import (
    SessionStartEvent as SessionStartEvent,
)
from meridian.lib.state.session_authority import (
    SessionStopEvent as SessionStopEvent,
)
from meridian.lib.state.session_authority import (
    SessionUpdateEvent as SessionUpdateEvent,
)
from meridian.lib.state.session_authority import (
    SourceModelSelectionEvent as SourceModelSelectionEvent,
)
from meridian.lib.state.session_authority import (
    UnresolvedBoundary as UnresolvedBoundary,
)
from meridian.lib.state.session_authority import (
    V4AttemptEvent as V4AttemptEvent,
)
from meridian.lib.state.session_authority import (
    project_session_event as project_session_event,
)


def _append_session_event(
    data_path: Path,
    event: SessionEvent | SessionModelObservationEvent,
    *,
    exclude_none: bool = False,
) -> None:
    paths = RuntimePaths.from_root_dir(data_path.parent)
    with _sessions_transaction(paths) as transaction:
        _append_proposed_row(data_path, transaction, event, exclude_none=exclude_none)


@dataclass
class _SessionTransaction:
    path: Path
    raw: bytes
    journal: JournalRead
    prepared: bool = False

    @property
    def snapshot(self) -> JournalSnapshot:
        return self.journal.snapshot

    def prepare(self) -> None:
        """Defer repair until the command has accepted its proposed effect."""
        if self.prepared:
            return
        if self.journal.tail != "empty":
            repaired = (
                self.raw + b"\n"
                if self.journal.tail == "complete_without_delimiter"
                else self.raw[: self.journal.valid_prefix_end]
            )
            HistoryChanges(self.path.parent).mark(HistorySource(kind="sessions"))
            atomic_write_bytes(self.path, repaired)
        self.prepared = True


@contextmanager
def _sessions_transaction(
    paths: RuntimePaths, *, require_existing_root: bool = False, repair_tail: bool = True
):
    """Own lock order, one strict projection, repair, and durable confirmation."""
    with lock_file(paths.project_lifetime_flock, mode="shared"):
        if require_existing_root and not paths.root_dir.is_dir():
            raise FileNotFoundError(paths.root_dir)
        with (
            lock_file(HistoryChanges(paths.root_dir).mutation_lock, mode="shared"),
            lock_file(paths.sessions_flock),
        ):
            try:
                raw = paths.sessions_jsonl.read_bytes()
            except FileNotFoundError:
                raw = b""
            transaction = _SessionTransaction(paths.sessions_jsonl, raw, read_journal(raw))
            try:
                yield transaction
                if repair_tail:
                    transaction.prepare()
            finally:
                _confirm_sessions_durability(paths.sessions_jsonl)


def _confirm_sessions_durability(path: Path) -> None:
    """Confirm both file content and publication of its directory entry."""
    if path.exists():
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    fsync_directory(path.parent)


def _append_session_row(path: Path, event: BaseModel, *, exclude_none: bool = False) -> None:
    payload = event.model_dump(mode="json", exclude_none=exclude_none)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
    append_text_line(path, line)


class _SessionLockHandles(NamedTuple):
    session: IO[bytes]
    project_lifetime: IO[bytes]
    session_instance_id: str


_SESSION_LOCK_HANDLES: dict[tuple[Path, str], _SessionLockHandles] = {}


def _commit_attempt(
    runtime_root: Path,
    context: BeginIntent | BeginIntentV4,
    observation: BoundaryFact | BoundaryFactV4 | Refutation | RefutationV4 | None = None,
    *,
    for_input: bool = False,
) -> AttemptResult:
    """Coordinator-only persistence. Facts are not an authority-issuing API."""
    fact: AttemptFact = context if observation is None else observation
    paths = RuntimePaths.from_root_dir(runtime_root)
    with _sessions_transaction(paths) as transaction:
        snapshot = transaction.snapshot
        if observation is not None:
            state = snapshot.attempts.states.get((context.run_id, context.attempt_id))
            if state is None or state.begin.intent() != context:
                raise ValueError("observation has no matching recorded owner context")
            if for_input:
                if not isinstance(observation, (BoundaryFact, BoundaryFactV4)):
                    raise ValueError("input unresolved: an accepted entry is required")
                if not eligible_input_entry(
                    snapshot.attempts, snapshot.identity, context, observation
                ):
                    raise ValueError("input unresolved: attempt closed or superseded")
        decision = plan_attempt(snapshot.attempts, snapshot.identity, fact)
        if isinstance(decision, NeedChat):
            chat = _allocate_binding_chat_id(paths, transaction)
            decision = plan_attempt(snapshot.attempts, snapshot.identity, fact, assigned_chat=chat)
        assert not isinstance(decision, NeedChat)
        if for_input and not isinstance(decision.result, AcceptedBoundary):
            raise ValueError("input unresolved: native boundary is not currently eligible")
        if decision.row is not None:
            _append_authority_event(paths.sessions_jsonl, transaction, decision.row)
        return decision.result


def _allocate_binding_chat_id(paths: RuntimePaths, transaction: _SessionTransaction) -> ChatId:
    """Allocate above every occupied or previously reserved canonical reference."""
    with lock_file(paths.session_id_counter_flock):
        current = _read_session_counter(paths)
        high_water = max(current, transaction.snapshot.identity.max_canonical_number)
        transaction.prepare()
        result = ChatId(f"c{high_water + 1}")
        atomic_write_text(paths.session_id_counter, f"{high_water + 1}\n")
        return result


def _append_proposed_row(
    path: Path,
    transaction: _SessionTransaction,
    event: SessionEvent | SessionModelObservationEvent,
    *,
    exclude_none: bool = False,
) -> None:
    snapshot = transaction.snapshot
    decision = plan_identity(snapshot.identity, event)
    if isinstance(decision, NoOp):
        return
    assert isinstance(decision, IdentityDelta)
    if isinstance(event, (SessionUpdateEvent, SessionModelSelectionEvent)):
        snapshot.metadata.validate_startup(event)
    transaction.prepare()
    HistoryChanges(path.parent).mark(HistorySource(kind="sessions"))
    _append_session_row(path, event, exclude_none=exclude_none)


def _append_authority_event(
    path: Path, transaction: _SessionTransaction, event: SessionAttemptEvent | V4AttemptEvent
) -> None:
    """Append through the active transaction and resolve ambiguous writes."""
    try:
        transaction.prepare()
        HistoryChanges(path.parent).mark(HistorySource(kind="sessions"))
        _append_session_row(path, event)
    except OSError:
        # Exceptional reread only: visibility is not a commit without a fresh barrier.
        recovered = read_journal(path.read_bytes()).snapshot
        state = recovered.attempts.states.get((event.run_id, event.attempt_id))
        committed = state is not None and event in (
            state.begin,
            state.entry,
            state.exit,
            state.invalidation,
        )
        if isinstance(event, LocatorConflictEvent):
            binding = recovered.identity.native_bindings.get(native_key_tuple(event.fact.key))
            committed = binding is not None and binding.conflict == event
        if not committed:
            raise
        _confirm_sessions_durability(path)


def get_native_session_key(runtime_root: Path, chat_id: str) -> NativeSessionKey | None:
    """Return diagnostic identity only; never use this key as a continuation credential."""
    paths = RuntimePaths.from_root_dir(runtime_root)
    with _sessions_transaction(paths) as transaction:
        snapshot = transaction.snapshot
        normalized = ChatId(normalize_optional_identity(chat_id) or "")
        key = snapshot.identity.chat_to_key.get(normalized)
        binding = (
            snapshot.identity.native_bindings.get(native_key_tuple(key))
            if key is not None
            else None
        )
        # V4 carries exact-source provenance, but until its purpose-aware resolver
        # is enabled the legacy key-only getter must not turn it into authority.
        if binding is not None and binding.protocol == "v4":
            return None
        return key


def get_native_binding(runtime_root: Path, chat_id: str) -> NativeBindingStatus:
    """Return binding status, converting invalid persisted authority to typed refusal."""
    try:
        return _get_native_binding(runtime_root, chat_id)
    except InvalidSessionJournal:
        # Replay rejected the whole snapshot; do not expose a prefix key or repair bytes.
        normalized = ChatId(normalize_optional_identity(chat_id) or "")
        return UnavailableBinding(normalized, "authority_invalid")


@dataclass(frozen=True)
class NativeIdCandidate:
    """One recorded claim for a bare native ID, with its source provenance."""

    harness: str
    store: str | None
    native_session_id: str
    chat_id: str
    provenance: Literal["v4", "v3", "legacy_lifecycle"]
    protocol: str | None
    pin: str | None
    blocked: str | None
    generation: str | None = None


@dataclass(frozen=True)
class NativeIdMatches:
    candidates: tuple[NativeIdCandidate, ...]


@dataclass(frozen=True)
class NativeIdNoMatch:
    """A complete, valid authority replay had no claim for the ID."""


@dataclass(frozen=True)
class NativeIdUnavailable:
    reason: Literal["authority_invalid", "authority_io"]


@dataclass(frozen=True)
class NativeIdAmbiguous:
    candidates: tuple[NativeIdCandidate, ...]


type NativeIdLookup = NativeIdMatches | NativeIdNoMatch | NativeIdUnavailable | NativeIdAmbiguous


@dataclass(frozen=True)
class NativeSourceUseSnapshot:
    """One strict, non-repairing authority projection for one source decision."""

    journal: JournalSnapshot

    def replay_model_facts(
        self, source: RecordedNativeSource
    ) -> ReplayModelFacts | FactsUnavailable:
        """Project exact v2 intent from this retained fold; performs no reads."""
        if not requested_source_eligible(self.journal.identity, source):
            return FactsUnavailable("source_not_eligible")
        matching: list[BoundModelIntent] = []
        for fact in self.journal.metadata.model_intents:
            if fact.correlation == "legacy_unscoped":
                continue
            event = fact.event
            if not isinstance(event, SourceModelSelectionEvent) or event.source != source:
                continue
            if fact.correlation == "ambiguous":
                return FactsUnavailable("source_conflict")
            start_key = (event.chat_id, event.session_instance_id, event.harness)
            if start_key in self.journal.metadata.conflicting_start_keys:
                return FactsUnavailable("source_conflict")
            if event.startup_attempt_id is not None and len(
                self.journal.metadata.update_ids.get(
                    (event.chat_id, event.session_instance_id, event.startup_attempt_id), ()
                )
            ) > 1:
                return FactsUnavailable("source_conflict")
            matching.append(fact)
        invocations: dict[str, BoundModelIntent] = {}
        seeds: list[BoundModelIntent] = []
        for fact in matching:
            event = fact.event
            assert isinstance(event, SourceModelSelectionEvent)
            if event.kind == "initial_seed":
                seeds.append(fact)
            elif event.spawn_id is not None and event.spawn_id not in invocations:
                invocations[event.spawn_id] = fact
        latest = max(invocations.values(), key=lambda item: item.journal_ordinal, default=None)
        return ReplayModelFacts(latest, seeds[0] if seeds else None)

    def binding(self, chat_id: str) -> NativeBindingStatus:
        return _native_binding_from_snapshot(self.journal, chat_id)

    def candidates(self, native_session_id: str, *, harness: str | None = None) -> NativeIdLookup:
        return _native_id_candidates_from_snapshot(
            self.journal, native_session_id, harness=harness
        )

    def selected_chat_candidates(
        self, chat_id: str, native_session_id: str, *, harness: str | None = None
    ) -> NativeIdLookup:
        lookup = self.candidates(native_session_id, harness=harness)
        if isinstance(lookup, NativeIdUnavailable | NativeIdNoMatch):
            return lookup
        matches = tuple(item for item in lookup.candidates if item.chat_id == chat_id)
        if not matches:
            return NativeIdNoMatch()
        if len(matches) != 1:
            return NativeIdAmbiguous(matches)
        return NativeIdMatches(matches)


def read_native_source_use_snapshot(
    runtime_root: Path,
) -> NativeSourceUseSnapshot | NativeIdUnavailable:
    """Read/fold authority once under the normal locks without repairing bytes.

    The final fsync barrier is retained even though this path is read-only: an
    accepted negative or positive policy decision must not bypass the journal's
    durability contract. A complete final row without LF is valid and remains
    byte-identical; malformed or torn authority and expected I/O failures refuse.
    """
    paths = RuntimePaths.from_root_dir(runtime_root)
    try:
        with _sessions_transaction(paths, repair_tail=False) as transaction:
            if transaction.journal.tail == "torn":
                raise InvalidSessionJournal("Torn sessions.jsonl tail")
            snapshot = transaction.snapshot
    except InvalidSessionJournal:
        return NativeIdUnavailable("authority_invalid")
    except OSError:
        return NativeIdUnavailable("authority_io")
    return NativeSourceUseSnapshot(snapshot)


def lookup_native_id_candidates(
    runtime_root: Path, native_session_id: str, *, harness: str | None = None
) -> NativeIdLookup:
    """Strictly look up every journal claim for a native ID in one locked replay.

    No filesystem discovery or SQLite projection participates. Lifecycle claims
    are retained as evidence but never promoted to v4 binding authority.
    """
    normalized_id = normalize_optional_identity(native_session_id)
    if normalized_id is None:
        raise ValueError("native session ID must be non-empty")
    harness_filter = harness.strip().lower() if harness is not None else None
    paths = RuntimePaths.from_root_dir(runtime_root)
    try:
        with _sessions_transaction(paths, repair_tail=False) as transaction:
            if transaction.journal.tail == "torn":
                raise InvalidSessionJournal("Torn sessions.jsonl tail")
            snapshot = transaction.snapshot
    except InvalidSessionJournal:
        return NativeIdUnavailable("authority_invalid")
    except OSError:
        return NativeIdUnavailable("authority_io")

    return _native_id_candidates_from_snapshot(snapshot, normalized_id, harness=harness_filter)


def _native_id_candidates_from_snapshot(
    snapshot: JournalSnapshot, normalized_id: str, *, harness: str | None = None
) -> NativeIdLookup:
    harness_filter = harness.strip().lower() if harness is not None else None

    candidates: list[NativeIdCandidate] = []
    for binding in snapshot.identity.native_bindings.values():
        key = binding.key
        if key.native_session_id != normalized_id or (
            harness_filter is not None and key.harness != harness_filter
        ):
            continue
        source = binding.source
        pin = (
            "pinned"
            if isinstance(source, Pinned)
            else ("pending" if isinstance(source, Pending) else "unobserved")
        )
        blocked = (
            "source_conflict"
            if binding.conflict is not None
            else "locator_unrecorded"
            if isinstance(source, LocatorUnrecorded)
            else None
        )
        candidates.append(
            NativeIdCandidate(
                key.harness,
                key.store,
                normalized_id,
                str(binding.chat_id),
                "v4" if binding.protocol == "v4" else "v3",
                binding.protocol,
                pin,
                blocked,
            )
        )

    all_lifecycle_claims = snapshot.lifecycle_claims
    for record in all_lifecycle_claims:
        record_harness = record.harness
        if harness_filter is not None and record_harness != harness_filter:
            continue
        for alias in record.harness_session_ids:
            if alias != normalized_id:
                continue
            candidates.append(
                NativeIdCandidate(
                    record_harness,
                    None,
                    normalized_id,
                    record.chat_id,
                    "legacy_lifecycle",
                    None,
                    None,
                    "legacy_unverified",
                    record.generation,
                )
            )

    # A harness filter narrows matching candidates, not contradictory facts
    # recorded against the selected chat. Preserve those contradictions on the
    # pinned candidate so a singleton cannot be mistaken for a clean claim.
    candidates = [
        replace(candidate, blocked="selected_chat_conflict")
        if candidate.provenance in {"v3", "v4"}
        and candidate.blocked is None
        and any(
            claim.chat_id == candidate.chat_id
            and (
                any(alias != candidate.native_session_id for alias in claim.harness_session_ids)
                or (
                    candidate.native_session_id in claim.harness_session_ids
                    and claim.harness != candidate.harness
                )
            )
            for claim in all_lifecycle_claims
        )
        else candidate
        for candidate in candidates
    ]

    # Keep each generation/authority claim: a shared chat does not erase the
    # historical and legacy evidence needed by strict callers.
    matches = tuple(candidates)
    if not matches:
        return NativeIdNoMatch()
    ownership = {(item.harness, item.store, item.chat_id, item.provenance) for item in matches}
    if len(ownership) > 1 or len(matches) > 1:
        return NativeIdAmbiguous(matches)
    return NativeIdMatches(matches)


def _get_native_binding(runtime_root: Path, chat_id: str) -> NativeBindingStatus:
    """Read confirmed native binding provenance without checking native storage.

    A returned operational value is journal authority only. In particular, a
    pin is not evidence that its file remains present or unchanged.
    """
    paths = RuntimePaths.from_root_dir(runtime_root)
    with _sessions_transaction(paths) as transaction:
        return _native_binding_from_snapshot(transaction.snapshot, chat_id)


def _native_binding_from_snapshot(snapshot: JournalSnapshot, chat_id: str) -> NativeBindingStatus:
    normalized = ChatId(normalize_optional_identity(chat_id) or "")
    ref = snapshot.identity.refs.get(normalized)
    if ref is None:
        return UnavailableBinding(normalized, "unknown_ref")
    if isinstance(ref, Historical):
        return UnavailableBinding(normalized, "historical")
    if isinstance(ref, ReferenceOnly):
        return UnavailableBinding(normalized, "reserved_or_reference_only")
    key = snapshot.identity.chat_to_key.get(normalized)
    if key is None:
        return UnavailableBinding(normalized, "legacy_unverified")
    binding = snapshot.identity.native_bindings.get(native_key_tuple(key))
    if binding is None or binding.chat_id != normalized:
        # Lifecycle rows and pre-authority data can occupy a cN, but never
        # acquire native authority from their harness-session ID.
        return UnavailableBinding(normalized, "legacy_unverified", key)
    if binding.conflict is not None:
        return UnavailableBinding(normalized, "source_conflict", key)
    if binding.protocol != "v4" or isinstance(binding.source, LocatorUnrecorded):
        return UnavailableBinding(normalized, "locator_unrecorded", key)

    guard = binding.store_guard
    store_guard = (
        AcquiredStoreGuard(guard.object, guard.store_event_id) if guard is not None else None
    )
    source = binding.source
    if isinstance(source, Unobserved):
        exposed_source = UnobservedSource("unobserved", source.observation)
    elif isinstance(source, Pending):
        exposed_source = PendingSource("pending", source.observation)
    else:
        assert isinstance(source, Pinned)
        exposed_source = PinnedSource("pinned", source.observation, source.event_id)
    return OperationalBinding(
        normalized,
        key,
        binding.binding_event_id,
        store_guard,
        exposed_source,
    )


def get_native_attempt_boundaries(
    runtime_root: Path, run_id: str, attempt_id: str
) -> AttemptBoundaries:
    """Invalidation never erases identity or boundary provenance."""
    paths = RuntimePaths.from_root_dir(runtime_root)
    with _sessions_transaction(paths) as transaction:
        snapshot = transaction.snapshot
        state = snapshot.attempts.states.get((run_id, attempt_id))
        if state is None:
            return AttemptBoundaries(None, None, False)
        invalidated = state.invalidation is not None
        entry_result = result_for_boundary(snapshot.identity, state, state.entry)
        exit_result = result_for_boundary(snapshot.identity, state, state.exit)

        return AttemptBoundaries(
            entry_result.chat_id if isinstance(entry_result, AcceptedBoundary) else None,
            exit_result.chat_id if isinstance(exit_result, AcceptedBoundary) else None,
            invalidated,
        )


type MaterializedCleanupScope = str


class StaleSessionCleanup(NamedTuple):
    cleaned_ids: tuple[str, ...]
    materialized_scopes: tuple[MaterializedCleanupScope, ...]


def _parse_event(payload: dict[str, Any]) -> SessionEvent | None:
    event_type = payload.get("event")
    try:
        if event_type == "historical_import":
            return SessionHistoricalEvent.model_validate(payload)
        if event_type == "start":
            return SessionStartEvent.model_validate(payload)
        if event_type == "stop":
            return SessionStopEvent.model_validate(payload)
        if event_type == "update":
            return SessionUpdateEvent.model_validate(payload)
        if event_type == "model_selection":
            version = payload.get("v", 1)
            if type(version) is int and version == 2:
                return SourceModelSelectionEvent.model_validate(payload)
            return SessionModelSelectionEvent.model_validate(payload)
    except ValidationError:
        return None
    return None


def _parse_model_observation(payload: dict[str, Any]) -> SessionModelObservationEvent | None:
    if payload.get("event") != "model_observation":
        return None
    return SessionModelObservationEvent.model_validate(payload)


def _session_lease_path(paths: RuntimePaths, chat_id: str) -> Path:
    return paths.sessions_dir / f"{chat_id}.lease.json"


def _read_session_lease_data(
    paths: RuntimePaths,
    chat_id: str,
) -> tuple[bool, str, int | None, float | None]:
    lease_path = _session_lease_path(paths, chat_id)
    if not lease_path.is_file():
        return (False, "", None, None)
    try:
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return (False, "", None, None)
    if not isinstance(payload, dict):
        return (False, "", None, None)
    payload_dict = cast("dict[str, Any]", payload)
    generation = payload_dict.get("session_instance_id")
    owner_pid = payload_dict.get("owner_pid")
    owner_created_at_epoch = payload_dict.get("owner_created_at_epoch")
    parsed_owner_pid = (
        owner_pid if isinstance(owner_pid, int) and not isinstance(owner_pid, bool) else None
    )
    parsed_owner_created_at_epoch = (
        float(owner_created_at_epoch)
        if isinstance(owner_created_at_epoch, int | float)
        and not isinstance(owner_created_at_epoch, bool)
        else None
    )
    if isinstance(generation, str):
        return (True, generation, parsed_owner_pid, parsed_owner_created_at_epoch)
    return (True, "", parsed_owner_pid, parsed_owner_created_at_epoch)


def _read_session_lease(paths: RuntimePaths, chat_id: str) -> tuple[bool, str]:
    lease_exists, generation, _owner_pid, _owner_birth = _read_session_lease_data(paths, chat_id)
    return (lease_exists, generation)


def _write_session_lease(paths: RuntimePaths, chat_id: str, session_instance_id: str) -> None:
    try:
        owner_created_at_epoch: float | None = psutil.Process(os.getpid()).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        owner_created_at_epoch = None
    payload = {
        "chat_id": chat_id,
        "owner_pid": os.getpid(),
        "owner_created_at_epoch": owner_created_at_epoch,
        "session_instance_id": session_instance_id,
    }
    atomic_write_text(
        _session_lease_path(paths, chat_id),
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
    )


def _session_instance_for_event(paths: RuntimePaths, runtime_root: Path, chat_id: str) -> str:
    held = _SESSION_LOCK_HANDLES.get(_session_lock_key(runtime_root, chat_id))
    if held is not None:
        return held.session_instance_id

    _, lease_session_instance_id = _read_session_lease(paths, chat_id)
    if lease_session_instance_id.strip():
        return lease_session_instance_id

    record = _records_by_session(runtime_root).get(chat_id)
    if record is None:
        return ""
    return record.session_instance_id


def _normalize_chat_id(chat_id: str) -> ChatId:
    """Normalize a required command reference before using it for state access."""

    normalized = normalize_optional_identity(chat_id)
    if normalized is None:
        raise ValueError("chat_id must not be empty")
    return ChatId(normalized)


def _read_session_counter(paths: RuntimePaths) -> int:
    if not paths.session_id_counter.is_file():
        return 0
    try:
        value = int(paths.session_id_counter.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise ValueError("Session ID reservation counter is corrupt") from exc
    if value < 0:
        raise ValueError("Session ID reservation counter is corrupt")
    return value


def reserve_chat_id(runtime_root: Path) -> str:
    paths = RuntimePaths.from_root_dir(runtime_root)
    with _sessions_transaction(paths) as transaction:
        return _allocate_binding_chat_id(paths, transaction)


def _records_by_session(runtime_root: Path) -> dict[str, SessionRecord]:
    paths = RuntimePaths.from_root_dir(runtime_root)
    records: dict[str, SessionRecord] = {}
    for event in read_events(paths.sessions_jsonl, _parse_event):
        project_session_event(records, event)
    return records


def _session_sort_key(chat_id: str) -> tuple[int, str]:
    if number := canonical_chat_number(chat_id):
        return (number, chat_id)
    return (10**9, chat_id)


def _session_lock_key(runtime_root: Path, chat_id: str) -> tuple[Path, str]:
    return (runtime_root.resolve(), chat_id)


def _release_session_lock(runtime_root: Path, chat_id: str) -> None:
    lock_data = _SESSION_LOCK_HANDLES.pop(_session_lock_key(runtime_root, chat_id), None)
    if lock_data is None:
        return
    release_file_lock(lock_data.session)
    release_file_lock(lock_data.project_lifetime)


def _discard_expected_session_lock(
    runtime_root: Path,
    chat_id: str,
    expected_generation: str,
) -> None:
    """Discard only the registry entry for the session generation being cleaned."""

    key = _session_lock_key(runtime_root, chat_id)
    expected = _SESSION_LOCK_HANDLES.get(key)
    if expected is None or not _generation_matches(
        expected.session_instance_id, expected_generation
    ):
        return
    if _SESSION_LOCK_HANDLES.get(key) is expected:
        del _SESSION_LOCK_HANDLES[key]


def start_session(
    runtime_root: Path,
    harness: str,
    harness_session_id: str,
    model: str,
    chat_id: str | None = None,
    params: tuple[str, ...] = (),
    agent: str = "",
    agent_path: str = "",
    skills: tuple[str, ...] = (),
    skill_paths: tuple[str, ...] = (),
    forked_from_chat_id: str | None = None,
    forked_from_history_id: uuid.UUID | None = None,
    control_root: str | None = None,
    task_cwd: str | None = None,
    execution_cwd: str | None = None,
    claude_config_dir: str | None = None,
    kind: Literal["primary", "spawn"] = "spawn",
    spawn_id: str | None = None,
    model_selection_protocol: Literal[1] | None = None,
) -> str:
    """Append a session start event and acquire a lifetime session lock."""

    normalized_chat_id = normalize_optional_identity(chat_id)
    normalized_forked_from_chat_id = normalize_optional_identity(forked_from_chat_id)
    paths = RuntimePaths.from_root_dir(runtime_root)
    project_lifetime_handle = acquire_file_lock(paths.project_lifetime_flock, mode="shared")
    resolved_chat_id = normalized_chat_id or ""
    handle: IO[bytes] | None = None
    session_instance_id = uuid.uuid4().hex
    try:
        started_at = utc_now_iso()
        if not resolved_chat_id:
            resolved_chat_id = reserve_chat_id(runtime_root)
        lock_path = paths.sessions_dir / f"{resolved_chat_id}.lock"
        handle = acquire_file_lock(lock_path)
        event = SessionStartEvent(
            chat_id=ChatId(resolved_chat_id),
            kind=kind,
            harness=harness,
            harness_session_id=HarnessSessionId(harness_session_id),
            control_root=normalize_path_for_write(control_root),
            task_cwd=normalize_path_for_write(task_cwd),
            execution_cwd=normalize_path_for_write(execution_cwd),
            claude_config_dir=claude_config_dir,
            model=model,
            agent=agent,
            agent_path=agent_path,
            skills=skills,
            skill_paths=skill_paths,
            params=params,
            session_instance_id=session_instance_id,
            started_at=started_at,
            forked_from_chat_id=(
                ChatId(normalized_forked_from_chat_id)
                if normalized_forked_from_chat_id is not None
                else None
            ),
            spawn_id=spawn_id,
            forked_from_history_id=forked_from_history_id,
            model_selection_protocol=model_selection_protocol,
        )
        with lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"):
            # Chat-only callers select the current generation. Resolved references
            # carry their exact portable ancestor and must never be re-resolved.
            if normalized_forked_from_chat_id and forked_from_history_id is None:
                source = get_session_record(runtime_root, normalized_forked_from_chat_id)
                event = event.model_copy(
                    update={"forked_from_history_id": source.history_id if source else None}
                )
            spawn_snapshot = None
            if spawn_id is not None:
                from meridian.lib.state.spawn.repository import read_state

                # Read identity inputs without mutating the spawn. The journal is
                # authoritative; a rejected append must leave its mirror untouched.
                spawn_snapshot = read_state(paths.spawns_dir, spawn_id)
                if spawn_snapshot is not None:
                    event = event.model_copy(
                        update={
                            "history_id": spawn_snapshot.history_id,
                            "forked_from_history_id": event.forked_from_history_id
                            or spawn_snapshot.forked_from_history_id,
                        }
                    )
            _append_session_event(paths.sessions_jsonl, event)
            _write_session_lease(paths, resolved_chat_id, session_instance_id)
            if spawn_id is not None and spawn_snapshot is not None:
                from meridian.lib.state.spawn.model import SpawnRecord
                from meridian.lib.state.spawn.repository import write_state_locked

                def bind(current: SpawnRecord) -> SpawnRecord:
                    return current.model_copy(
                        update={
                            "chat_id": event.chat_id,
                            "session_instance_id": event.session_instance_id,
                            "forked_from_history_id": event.forked_from_history_id,
                        }
                    )

                # Repairable from the committed session identity: this mirror is
                # deliberately published only after the journal's durability barrier.
                write_state_locked(paths.spawns_dir, spawn_id, bind, allow_terminal_overwrite=True)
    except Exception:
        if handle is not None:
            release_file_lock(handle)
        release_file_lock(project_lifetime_handle)
        raise

    _SESSION_LOCK_HANDLES[_session_lock_key(runtime_root, resolved_chat_id)] = _SessionLockHandles(
        session=handle,
        project_lifetime=project_lifetime_handle,
        session_instance_id=session_instance_id,
    )
    return resolved_chat_id


def stop_session(runtime_root: Path, chat_id: str) -> None:
    """Append a session stop event and release the lifetime session lock."""

    chat_ref = _normalize_chat_id(chat_id)
    paths = RuntimePaths.from_root_dir(runtime_root)
    session_instance_id = _session_instance_for_event(paths, runtime_root, chat_ref)
    event = SessionStopEvent(
        chat_id=chat_ref,
        session_instance_id=session_instance_id,
        stopped_at=utc_now_iso(),
    )
    _append_session_event(
        paths.sessions_jsonl,
        event,
        exclude_none=True,
    )
    _session_lease_path(paths, chat_ref).unlink(missing_ok=True)
    _release_session_lock(runtime_root, chat_ref)


def update_session_harness_id(
    runtime_root: Path,
    chat_id: str,
    harness_session_id: str,
    *,
    session_instance_id: str | None = None,
    startup_attempt_id: str | None = None,
) -> None:
    """Append a session update event carrying the resolved harness session ID."""

    if startup_attempt_id is not None and session_instance_id is None:
        raise ValueError("startup identity requires a captured session generation")
    chat_ref = _normalize_chat_id(chat_id)
    paths = RuntimePaths.from_root_dir(runtime_root)
    event = SessionUpdateEvent(
        chat_id=chat_ref,
        harness_session_id=HarnessSessionId(harness_session_id),
        session_instance_id=(
            session_instance_id
            if session_instance_id is not None
            else _session_instance_for_event(paths, runtime_root, chat_ref)
        ),
        startup_attempt_id=startup_attempt_id,
    )
    _append_session_event(
        paths.sessions_jsonl,
        event,
        exclude_none=True,
    )


def update_session_work_id(runtime_root: Path, chat_id: str, work_id: str | None) -> None:
    """Set or clear the active work item for a session."""

    chat_ref = _normalize_chat_id(chat_id)
    paths = RuntimePaths.from_root_dir(runtime_root)
    normalized_work_id = work_id.strip() if work_id is not None else ""
    event = SessionUpdateEvent(
        chat_id=chat_ref,
        harness_session_id=None,
        session_instance_id=_session_instance_for_event(paths, runtime_root, chat_ref),
        active_work_id=normalized_work_id,
    )
    _append_session_event(
        paths.sessions_jsonl,
        event,
        exclude_none=True,
    )


def update_session_spawn_id(runtime_root: Path, chat_id: str, spawn_id: str) -> None:
    """Record the canonical primary spawn relationship for a session."""

    chat_ref = _normalize_chat_id(chat_id)
    paths = RuntimePaths.from_root_dir(runtime_root)
    from meridian.lib.state.spawn.repository import read_state

    spawn = read_state(paths.spawns_dir, spawn_id.strip(), include_prompt=False)
    event = SessionUpdateEvent(
        chat_id=chat_ref,
        harness_session_id=None,
        session_instance_id=_session_instance_for_event(paths, runtime_root, chat_ref),
        spawn_id=spawn_id.strip(),
        history_id=spawn.history_id if spawn is not None else None,
    )
    _append_session_event(
        paths.sessions_jsonl,
        event,
        exclude_none=True,
    )


def update_session_claude_config_dir(
    runtime_root: Path,
    chat_id: str,
    claude_config_dir: str,
) -> None:
    """Append a session update event carrying the isolated Claude config dir."""

    chat_ref = _normalize_chat_id(chat_id)
    paths = RuntimePaths.from_root_dir(runtime_root)
    event = SessionUpdateEvent(
        chat_id=chat_ref,
        harness_session_id=None,
        session_instance_id=_session_instance_for_event(paths, runtime_root, chat_ref),
        claude_config_dir=claude_config_dir,
    )
    _append_session_event(
        paths.sessions_jsonl,
        event,
        exclude_none=True,
    )


def list_active_sessions(runtime_root: Path) -> list[str]:
    """Return session IDs with currently held `sessions/<id>.lock` locks."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    if not paths.sessions_dir.exists():
        return []

    active: list[str] = []
    for lock_path in paths.sessions_dir.glob("*.lock"):
        chat_id = lock_path.stem
        with try_lock_file(lock_path, reentrant=False) as handle:
            if handle is None:
                active.append(chat_id)
    return sorted(active, key=_session_sort_key)


def has_live_session_leases(runtime_root: Path) -> bool:
    """Return whether any session lease names a currently live owner process."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    if not paths.sessions_dir.exists():
        return False
    for lease_path in paths.sessions_dir.glob("*.lease.json"):
        chat_id = lease_path.name.removesuffix(".lease.json")
        _exists, _generation, owner_pid, owner_birth = _read_session_lease_data(paths, chat_id)
        if owner_pid is not None and is_process_alive_with_birth(owner_pid, owner_birth):
            return True
    return False


def is_session_lease_owner_alive(
    runtime_root: Path, chat_id: str, *, session_instance_id: str | None = None
) -> bool:
    """Check a live lease, optionally requiring the exact session generation."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    _exists, generation, owner_pid, owner_birth = _read_session_lease_data(paths, chat_id)
    if session_instance_id is not None and generation != session_instance_id:
        return False
    return owner_pid is not None and is_process_alive_with_birth(owner_pid, owner_birth)


def list_active_session_records(runtime_root: Path) -> list[SessionRecord]:
    """Return materialized records for active sessions."""

    records = _records_by_session(runtime_root)
    return [
        record
        for chat_id in list_active_sessions(runtime_root)
        if (record := records.get(chat_id)) is not None
    ]


def list_all_session_records(runtime_root: Path) -> list[SessionRecord]:
    """Return all materialized records, including stopped sessions."""

    return list(_records_by_session(runtime_root).values())


def get_session_record(runtime_root: Path, chat_id: str) -> SessionRecord | None:
    """Return a materialized record for one chat ID, if present."""

    return _records_by_session(runtime_root).get(_normalize_chat_id(chat_id))


def _bound_model_selections(
    events: list[SessionEvent],
) -> list[tuple[SessionModelSelectionEvent, str | None]]:
    identities: dict[tuple[str, str, str], set[str]] = {}
    for event in events:
        if isinstance(event, SessionUpdateEvent) and (
            event.startup_attempt_id is not None and event.harness_session_id is not None
        ):
            key = (event.chat_id, event.session_instance_id, event.startup_attempt_id)
            identities.setdefault(key, set()).add(event.harness_session_id)

    bound: list[tuple[SessionModelSelectionEvent, str | None]] = []
    for event in events:
        if not isinstance(event, SessionModelSelectionEvent):
            continue
        native_id: str | None = event.harness_session_id
        if native_id is None and event.startup_attempt_id is not None:
            observed = identities.get(
                (event.chat_id, event.session_instance_id, event.startup_attempt_id), set()
            )
            if len(observed) == 1:
                native_id = next(iter(observed))
        bound.append((event, native_id))
    return bound


def get_initial_model_selection(
    runtime_root: Path,
    harness: str,
    harness_session_id: str,
    *,
    source_chat_id: str | None = None,
) -> SessionModelSelectionEvent | None:
    """Read a legacy conversation's original value without seeding or replaying attempts."""
    from meridian.lib.state.spawn_store import get_spawn

    paths = RuntimePaths.from_root_dir(runtime_root)
    events = read_events(paths.sessions_jsonl, _parse_event)
    updates: dict[tuple[str, str], list[SessionUpdateEvent]] = {}
    for event in events:
        if isinstance(event, SessionUpdateEvent):
            updates.setdefault((event.chat_id, event.session_instance_id), []).append(event)
    starts = [
        event
        for event in events
        if isinstance(event, SessionStartEvent) and event.harness == harness
    ]
    start = next(
        (
            event
            for event in starts
            if event.harness_session_id == harness_session_id
            or any(
                update.harness_session_id == harness_session_id
                for update in updates.get((event.chat_id, event.session_instance_id), [])
            )
        ),
        None,
    )
    origin_chat_id = start.chat_id if start is not None else source_chat_id
    if origin_chat_id is not None:
        start = next((event for event in starts if event.chat_id == origin_chat_id), None)
    if start is None or start.model_selection_protocol is not None:
        return None
    generation_updates = updates.get((start.chat_id, start.session_instance_id), [])
    spawn_id = start.spawn_id or next(
        (update.spawn_id for update in generation_updates if update.spawn_id),
        None,
    )
    spawn = get_spawn(runtime_root, spawn_id) if spawn_id else None
    snapshot = spawn.launch_policy_snapshot if spawn is not None else None
    selection = ConversationModelSelection(
        requested_token=start.model or None,
        selected_token=start.model or None,
        selection_source="initial_launch",
    )
    if snapshot is not None:
        canonical = snapshot.model_selection_canonical_id or None
        executable = snapshot.model_selection_harness_model_id or None
        requested = snapshot.model_selection_requested_token or snapshot.model or None
        selected = snapshot.model_selection_selected_token or snapshot.model or None
        named = bool(requested and selected and canonical and executable)
        selection = ConversationModelSelection(
            requested_token=requested,
            selected_token=selected,
            canonical_model_id=canonical if named else None,
            harness_model_id=executable if named else None,
            model_mode=(
                "named"
                if named
                else "harness_default"
                if not snapshot.model and not canonical
                else None
            ),
            provider_constraint=(snapshot.model_selection_provider_constraint if named else None),
            selection_source="initial_launch",
            provenance=snapshot.field_provenance,
        )
    return SessionModelSelectionEvent(
        kind="initial_seed",
        harness=harness,
        harness_session_id=HarnessSessionId(harness_session_id),
        chat_id=start.chat_id,
        session_instance_id=start.session_instance_id,
        spawn_id=spawn_id,
        startup_attempt_id=None,
        recorded_at=utc_now_iso(),
        selection=selection,
    )


def get_model_selection(
    runtime_root: Path,
    harness: str,
    harness_session_id: str,
) -> ConversationModelSelection | None:
    """Read latest committed intent without changing historical session records."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    current: ConversationModelSelection | None = None
    seed: ConversationModelSelection | None = None
    seen_invocations: set[str] = set()
    for event, native_id in _bound_model_selections(
        read_events(paths.sessions_jsonl, _parse_event)
    ):
        if event.harness != harness or native_id != harness_session_id:
            continue
        if event.kind == "initial_seed":
            if seed is None:
                seed = event.selection
        elif event.spawn_id is not None and event.spawn_id not in seen_invocations:
            seen_invocations.add(event.spawn_id)
            current = event.selection
    return current if current is not None else seed


def record_model_selection(
    runtime_root: Path, event: SessionModelSelectionEvent | SourceModelSelectionEvent
) -> bool:
    """Durably append once per invocation/conversation; false means already recorded."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    with _sessions_transaction(paths, require_existing_root=True) as transaction:
        snapshot = transaction.snapshot
        metadata = snapshot.metadata
        source_start = metadata.starts.get(
            (event.chat_id, event.session_instance_id, event.harness)
        )
        if source_start is None:
            raise ValueError("selection has no matching captured session generation")
        if event.kind == "initial_seed" and source_start.model_selection_protocol is not None:
            raise ValueError("cannot seed a new-protocol session from prelaunch intent")
        if isinstance(event, SourceModelSelectionEvent):
            if event.startup_attempt_id is not None and not event.startup_attempt_id.strip():
                return False
            if source_start.spawn_id != event.spawn_id:
                raise ValueError("v2 selection spawn differs from captured start")
            if not requested_source_eligible(snapshot.identity, event.source):
                raise ValueError("v2 selection source is not the current exact pin")
            if (
                (event.chat_id, event.session_instance_id, event.harness)
                in metadata.conflicting_start_keys
            ):
                raise ValueError("v2 selection captured start is contradictory")
            source_key = (native_key_tuple(event.source.key), event.kind,
                          event.spawn_id if event.kind == "invocation_started" else event.chat_id)
            if source_key in metadata.exact_seen:
                prior = metadata.exact_seen[source_key]
                semantic = json.dumps(
                    {
                        "source": event.source.model_dump(mode="json"),
                        "generation": event.session_instance_id,
                        "selection": event.selection.model_dump(mode="json"),
                    }, separators=(",", ":"), sort_keys=True
                ).encode()
                if prior != semantic:
                    raise ValueError("contradictory exact model selection")
                metadata.validate_source_selection(event, committed_duplicate=True)
                return False
            try:
                metadata.validate_source_selection(event)
            except ValueError:
                if (
                    event.harness_session_id is None
                    and metadata.source_selection_conflicts(event)
                ):
                    return False
                if event.kind == "initial_seed" and any(
                    isinstance(fact.event, SourceModelSelectionEvent)
                    and fact.event.source == event.source
                    and fact.event.kind == "invocation_started"
                    for fact in metadata.model_intents
                ):
                    return False
                raise
        # Even a metadata no-op must not hide an attempted historical mutation.
        plan_identity(snapshot.identity, event)
        metadata.validate_startup(event)
        native_id = metadata.selection_native_id(event)
        if isinstance(event, SessionModelSelectionEvent) and native_id is not None and (
            (event.kind == "initial_seed" and (event.harness, native_id) in metadata.selections)
            or (
                event.kind == "invocation_started"
                and (event.harness, native_id, event.spawn_id) in metadata.invocations
            )
        ):
            if event.kind == "invocation_started" and native_id not in metadata.startup_ids.get(
                startup_key(event), frozenset()
            ):
                _append_proposed_row(
                    paths.sessions_jsonl,
                    transaction,
                    SessionUpdateEvent(
                        chat_id=event.chat_id,
                        session_instance_id=event.session_instance_id,
                        startup_attempt_id=event.startup_attempt_id,
                        harness_session_id=HarnessSessionId(native_id),
                    ),
                    exclude_none=True,
                )
            return False
        if (
            isinstance(event, SessionModelSelectionEvent)
            and event.kind == "invocation_started"
            and selection_startup_key(event) in metadata.startup_selections
        ):
            return False
        _append_proposed_row(paths.sessions_jsonl, transaction, event)
        return True


def record_model_observation(runtime_root: Path, event: SessionModelObservationEvent) -> bool:
    """Durably append an executed-model observation; false means already recorded.

    Independent of intent: this fact keys on ``(harness, harness_session_id)`` and
    may exist for native sessions Meridian did not start, so no matching
    ``SessionStartEvent`` generation is required. Dedupes against the latest
    observation for the identity to keep the JSONL bounded.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    with _sessions_transaction(paths, require_existing_root=True) as transaction:
        snapshot = transaction.snapshot
        previous = snapshot.metadata.observations.get((event.harness, event.harness_session_id))
        if previous == event.observed_model_token:
            return False
        _append_proposed_row(paths.sessions_jsonl, transaction, event)
        return True


def get_last_executed_model(
    runtime_root: Path,
    harness: str,
    harness_session_id: str,
) -> str | None:
    """Return the latest observed executed model token for a native session, or None."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    latest: str | None = None
    for event in read_events(paths.sessions_jsonl, _parse_model_observation):
        if event.harness == harness and event.harness_session_id == harness_session_id:
            latest = event.observed_model_token
    return latest


def list_active_sessions_for_work_id(runtime_root: Path, work_id: str) -> list[str]:
    """Return active session IDs currently attached to a work item."""

    normalized = work_id.strip()
    if not normalized:
        return []
    return [
        record.chat_id
        for record in list_active_session_records(runtime_root)
        if record.active_work_id == normalized
    ]


def get_session_records(runtime_root: Path, chat_ids: set[str]) -> list[SessionRecord]:
    """Return materialized records for a set of Meridian chat/session IDs."""

    if not chat_ids:
        return []
    records = _records_by_session(runtime_root)
    return [
        records[chat_id]
        for chat_id in sorted(
            {chat_id.strip() for chat_id in chat_ids if chat_id.strip()},
            key=_session_sort_key,
        )
        if chat_id in records
    ]


def get_last_session(runtime_root: Path) -> SessionRecord | None:
    """Return the most recently started session record in a state root."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    last_session_id: str | None = None
    for event in read_events(paths.sessions_jsonl, _parse_event):
        if not isinstance(event, SessionStartEvent):
            continue
        last_session_id = event.chat_id

    if last_session_id is None:
        return None
    return _records_by_session(runtime_root).get(last_session_id)


def resolve_session_ref(
    runtime_root: Path,
    ref: str,
    *,
    harness: str | None = None,
) -> SessionRecord | None:
    """Resolve a native ID in its harness namespace; reject ambiguous ownership."""

    normalized = ref.strip()
    if not normalized:
        return None

    matches = [
        record
        for record in list_session_generations(runtime_root)
        if normalized in record.harness_session_ids
        and (harness is None or record.harness == harness)
    ]
    if not matches:
        return None
    if len({record.harness for record in matches}) > 1:
        raise ValueError(
            "Native session reference is ambiguous across harnesses. "
            "Specify --harness or use a tracked chat/spawn reference."
        )
    return max(matches, key=lambda item: (item.started_at, _session_sort_key(item.chat_id)))


def get_session_active_work_id(runtime_root: Path, chat_id: str) -> str | None:
    """Return the active work item ID for a session, or None."""

    record = _records_by_session(runtime_root).get(chat_id)
    if record is None:
        return None
    return record.active_work_id


def get_session_harness_id(runtime_root: Path, chat_id: str) -> str | None:
    """Return harness session ID for a Meridian session ID."""

    record = _records_by_session(runtime_root).get(chat_id)
    if record is None:
        return None
    return record.harness_session_id


def get_session_harness_ids(runtime_root: Path, chat_id: str) -> tuple[str, ...]:
    """Return all harness session IDs observed for a Meridian session ID."""

    record = _records_by_session(runtime_root).get(chat_id)
    if record is None:
        return ()
    return record.harness_session_ids


def collect_active_chat_ids(project_root: Path) -> frozenset[str] | None:
    """Collect chat IDs with start events that lack a stop event."""

    from meridian.lib.state.paths import resolve_project_runtime_root_or_none

    try:
        runtime_root = resolve_project_runtime_root_or_none(project_root)
        if runtime_root is None:
            return frozenset()
        sessions_file = runtime_root / "sessions.jsonl"
        if not sessions_file.is_file():
            return frozenset()

        started: set[str] = set()
        stopped: set[str] = set()
        for event in read_events(sessions_file, _parse_event):
            if isinstance(event, SessionStartEvent):
                started.add(event.chat_id)
            elif isinstance(event, SessionStopEvent):
                stopped.add(event.chat_id)
        return frozenset(started - stopped)
    except OSError:
        return None


def cleanup_stale_sessions(runtime_root: Path) -> StaleSessionCleanup:
    """Stop and remove dead session locks left behind by crashed harnesses."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    if not paths.sessions_dir.exists():
        return StaleSessionCleanup(cleaned_ids=(), materialized_scopes=())

    with ExitStack() as lock_stack:
        lock_stack.enter_context(lock_file(paths.project_lifetime_flock, mode="shared"))
        stale: list[tuple[str, Path, IO[bytes]]] = []
        for lock_path in paths.sessions_dir.glob("*.lock"):
            chat_id = lock_path.stem
            try:
                handle = lock_stack.enter_context(lock_file(lock_path, timeout=0, reentrant=False))
            except TimeoutError:
                continue
            stale.append((chat_id, lock_path, handle))

        if not stale:
            return StaleSessionCleanup(cleaned_ids=(), materialized_scopes=())

        cleaned_ids: list[str] = []
        stale_cleanup_scopes: list[str] = []
        lease_data = {chat_id: _read_session_lease_data(paths, chat_id) for chat_id, _, _ in stale}
        process_alive = {
            chat_id: lease[2] is not None and is_process_alive_with_birth(lease[2], lease[3])
            for chat_id, lease in lease_data.items()
        }
        with _sessions_transaction(paths) as transaction:
            snapshot = transaction.snapshot
            records = dict(snapshot.lifecycle)
            stopped_at = utc_now_iso()
            for chat_id, _lock_path, _ in stale:
                existing = records.get(chat_id)
                (
                    lease_exists,
                    lease_session_instance_id,
                    lease_owner_pid,
                    _lease_owner_birth,
                ) = lease_data[chat_id]
                if (
                    existing is not None
                    and existing.kind == "primary"
                    and lease_exists
                    and lease_owner_pid is not None
                    and process_alive[chat_id]
                ):
                    continue
                stop_session_instance_id = lease_session_instance_id
                if not lease_exists and existing is not None:
                    stop_session_instance_id = existing.session_instance_id
                if (
                    existing is not None
                    and existing.stopped_at is None
                    and (
                        not lease_exists
                        or _generation_matches(
                            existing.session_instance_id,
                            lease_session_instance_id,
                        )
                    )
                ):
                    _append_proposed_row(
                        paths.sessions_jsonl,
                        transaction,
                        SessionStopEvent(
                            chat_id=ChatId(chat_id),
                            session_instance_id=stop_session_instance_id,
                            stopped_at=stopped_at,
                        ),
                        exclude_none=True,
                    )
                    records[chat_id] = existing.model_copy(update={"stopped_at": stopped_at})

                should_clean = (
                    existing is None or existing.stopped_at is not None or not lease_exists
                )
                if existing is not None and existing.stopped_at is None:
                    should_clean = not lease_exists or _generation_matches(
                        existing.session_instance_id, lease_session_instance_id
                    )
                if not should_clean:
                    continue

                if existing is not None and existing.harness.strip():
                    stale_cleanup_scopes.append(existing.harness.strip())
                cleaned_ids.append(chat_id)
                cleaned_generation = (
                    lease_session_instance_id
                    if lease_exists
                    else existing.session_instance_id
                    if existing is not None
                    else ""
                )
                _discard_expected_session_lock(runtime_root, chat_id, cleaned_generation)
        for chat_id in cleaned_ids:
            _session_lease_path(paths, chat_id).unlink(missing_ok=True)

        cleaned_id_set = frozenset(cleaned_ids)
        for chat_id, lock_path, handle in stale:
            if chat_id in cleaned_id_set:
                unlink_validated_lock(lock_path, handle)

    return StaleSessionCleanup(
        cleaned_ids=tuple(sorted(cleaned_ids, key=_session_sort_key)),
        materialized_scopes=tuple(sorted(set(stale_cleanup_scopes))),
    )


def append_historical_session(
    runtime_root: Path,
    record: SessionRecord,
    *,
    history_id: uuid.UUID,
) -> None:
    """Commit the exact plan-backed historical identity from a published restore."""
    paths = RuntimePaths.from_root_dir(runtime_root)
    changes = HistoryChanges(runtime_root)
    plan_path = runtime_root / "history-archives" / "restores" / f"{history_id}.json"
    # The history gate serializes plan lifetime and aggregate publication. It is
    # reentrant for restore_archive, and also protects direct API callers.
    with lock_file(paths.project_lifetime_flock, mode="shared"), lock_file(changes.mutation_lock):
        plan = _validate_restore_plan(plan_path, history_id, record)
        # Source/aggregate verification must precede sessions_flock. A plan by
        # itself is not authority to consume a counter reservation.
        from meridian.lib.state.retention_archive import ArchivedRecord, verified_source
        from meridian.lib.state.retention_restore import _verify_existing

        destination = paths.spawns_dir / plan.local_id
        try:
            archived = ArchivedRecord.model_validate_json(
                (destination / "record.json").read_bytes()
            )
        except (OSError, ValidationError) as exc:
            raise ValueError("restore plan has no published inert aggregate") from exc
        if archived.history_id != history_id or archived.portable_digest != plan.portable_digest:
            raise ValueError("restore aggregate does not match its durable plan")
        with verified_source(destination):
            _verify_existing(destination, archived, pending_session=record)

        with _sessions_transaction(paths) as transaction:
            snapshot = transaction.snapshot
            # Re-read the durable file after sessions_flock: its identity must
            # still match the plan whose aggregate was checked above.
            if _validate_restore_plan(plan_path, history_id, record) != plan:
                raise ValueError("restore plan changed before historical session commit")
            event = SessionHistoricalEvent(record=record)
            decision = plan_identity(snapshot.identity, event)
            if isinstance(decision, NoOp):
                return
            with lock_file(paths.session_id_counter_flock):
                counter = _read_session_counter(paths)
            number = canonical_chat_number(record.chat_id)
            if not number or number > counter:
                raise ValueError("restore plan alias was not previously reserved")
            _append_proposed_row(paths.sessions_jsonl, transaction, event)


def _validate_restore_plan(plan_path: Path, history_id: uuid.UUID, record: SessionRecord) -> Any:
    """Load the typed plan only from its runtime-derived canonical location."""
    from meridian.lib.state.retention_restore import RestorePlan

    try:
        plan = RestorePlan.model_validate_json(plan_path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise ValueError("historical import has no valid durable restore plan") from exc
    if (
        plan_path.name != f"{history_id}.json"
        or record.history_id != history_id
        or plan.chat_id != record.chat_id
        or plan.local_id != record.spawn_id
        or plan.generation != record.session_instance_id
        or plan.session != record
        or re.fullmatch(r"[0-9a-f]{64}", plan.portable_digest, flags=re.ASCII) is None
    ):
        raise ValueError("restore plan does not own this historical session alias")
    return plan


def list_session_generations(runtime_root: Path) -> tuple[SessionRecord, ...]:
    """All generations, including historical and legacy starts, in source order."""
    generations: dict[tuple[str, str], dict[str, SessionRecord]] = {}
    latest_blank: dict[str, str] = {}
    for ordinal, event in enumerate(
        read_events(RuntimePaths.from_root_dir(runtime_root).sessions_jsonl, _parse_event)
    ):
        generation = event.session_instance_id
        if not generation:
            if isinstance(event, SessionStartEvent):
                latest_blank[event.chat_id] = f"legacy:{ordinal}"
            generation = latest_blank.get(event.chat_id, "")
        rows = generations.setdefault((event.chat_id, generation), {})
        project_session_event(rows, event)
    return tuple(record for rows in generations.values() for record in rows.values())
