"""Pure session journal schemas and projection; no storage or harness access.

Every authority consumer uses this ordered, normalized identity fold. Attempt
policy is shared by live proposals and strict replay, not owner qualification.
"""

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, Literal, NamedTuple, Self, assert_never, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from meridian.lib.core.types import (
    ChatId,
    HarnessSessionId,
    OptionalPersistedChatId,
    OptionalPersistedHarnessSessionId,
    PersistedChatId,
)


class SessionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat_id: PersistedChatId
    history_id: uuid.UUID | None = None
    record_mode: Literal["live", "historical"] = "live"
    kind: Literal["primary", "spawn"]
    harness: str
    harness_session_id: OptionalPersistedHarnessSessionId
    control_root: str | None = None
    task_cwd: str | None = None
    execution_cwd: str | None = None
    claude_config_dir: str | None = None
    harness_session_ids: tuple[HarnessSessionId, ...]
    model: str
    agent: str
    agent_path: str
    skills: tuple[str, ...]
    skill_paths: tuple[str, ...]
    params: tuple[str, ...]
    started_at: str
    stopped_at: str | None
    session_instance_id: str = ""
    active_work_id: str | None = None
    forked_from_chat_id: OptionalPersistedChatId = None
    forked_from_history_id: uuid.UUID | None = None
    spawn_id: str | None = None


class SessionStartEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    history_id: uuid.UUID | None = None
    v: Literal[1] = 1
    event: Literal["start"] = "start"
    chat_id: PersistedChatId
    kind: Literal["primary", "spawn"] = "spawn"
    harness: str
    harness_session_id: OptionalPersistedHarnessSessionId
    control_root: str | None = None
    task_cwd: str | None = None
    execution_cwd: str | None = None
    claude_config_dir: str | None = None
    model: str
    agent: str = ""
    agent_path: str = ""
    skills: tuple[str, ...] = ()
    skill_paths: tuple[str, ...] = ()
    params: tuple[str, ...] = ()
    session_instance_id: str = ""
    started_at: str
    forked_from_chat_id: OptionalPersistedChatId = None
    forked_from_history_id: uuid.UUID | None = None
    spawn_id: str | None = None
    model_selection_protocol: Literal[1] | None = None


class SessionStopEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    v: Literal[1] = 1
    event: Literal["stop"] = "stop"
    chat_id: PersistedChatId
    session_instance_id: str = ""
    stopped_at: str | None = None


class SessionUpdateEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    v: Literal[1] = 1
    event: Literal["update"] = "update"
    chat_id: PersistedChatId
    harness_session_id: OptionalPersistedHarnessSessionId = None
    session_instance_id: str = ""
    claude_config_dir: str | None = None
    active_work_id: str | None = None
    spawn_id: str | None = None
    history_id: uuid.UUID | None = None
    startup_attempt_id: str | None = None


class SessionHistoricalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event: Literal["historical_import"] = "historical_import"
    record: SessionRecord

    @property
    def chat_id(self) -> ChatId:
        return self.record.chat_id

    @property
    def session_instance_id(self) -> str:
        return self.record.session_instance_id


class ConversationModelSelection(BaseModel):
    """Meridian-selected intent, not an observation of an executed model."""

    model_config = ConfigDict(frozen=True)

    requested_token: str | None = None
    selected_token: str | None = None
    canonical_model_id: str | None = None
    harness_model_id: str | None = None
    model_mode: Literal["named", "harness_default"] | None = None
    provider_constraint: str | None = None
    selection_source: Literal[
        "explicit_override",
        "recorded_selection",
        "observed_last_used",
        "initial_launch",
        "unknown",
    ]
    provenance: dict[str, str] = Field(default_factory=dict)

    @property
    def literal_model(self) -> bool:
        return self.canonical_model_id is not None or self.model_mode == "harness_default"

    @property
    def routing_token(self) -> str | None:
        if self.selection_source == "explicit_override":
            return self.requested_token
        if self.selection_source == "recorded_selection":
            return self.canonical_model_id or self.selected_token
        return self.canonical_model_id or self.requested_token

    @property
    def mars_model(self) -> str | None:
        token = self.routing_token
        if self.provider_constraint and self.literal_model and token:
            return f"{self.provider_constraint}/{token}"
        return token

    @model_validator(mode="after")
    def validate_model_mode(self) -> Self:
        if self.model_mode == "named" and not (
            self.requested_token
            and self.selected_token
            and self.canonical_model_id
            and self.harness_model_id
        ):
            raise ValueError("named selection requires tokens and canonical/executable identities")
        if self.model_mode == "harness_default" and (
            self.canonical_model_id or self.harness_model_id or self.provider_constraint
        ):
            raise ValueError("harness-default selection cannot carry a named model")
        return self


class SessionModelSelectionEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    v: Literal[1] = 1
    event: Literal["model_selection"] = "model_selection"
    kind: Literal["initial_seed", "invocation_started"]
    harness: str
    harness_session_id: OptionalPersistedHarnessSessionId
    chat_id: PersistedChatId
    session_instance_id: str
    spawn_id: str | None
    startup_attempt_id: str | None
    recorded_at: str
    selection: ConversationModelSelection

    @model_validator(mode="after")
    def validate_invocation_identity(self) -> Self:
        if self.kind == "invocation_started" and not (
            self.spawn_id
            and self.session_instance_id
            and self.startup_attempt_id
            and self.selection.model_mode is not None
        ):
            raise ValueError(
                "started selection requires invocation identity and resolved model mode"
            )
        if self.kind == "initial_seed" and self.harness_session_id is None:
            raise ValueError("initial seed requires a native conversation identity")
        return self


class SessionModelObservationEvent(BaseModel):
    """Observed executed model for a native session, independent of intent.

    Keyed by ``(harness, harness_session_id)`` — no chat/generation binding. This
    records what the harness actually last executed, which may diverge from
    Meridian's selected intent and can exist for native sessions Meridian never
    started. Parallel JSONL fact, not a session-lifecycle event: only observation
    readers parse it.
    """

    model_config = ConfigDict(frozen=True)

    v: Literal[1] = 1
    event: Literal["model_observation"] = "model_observation"
    harness: str
    harness_session_id: HarnessSessionId
    observed_model_token: str
    observed_source: Literal["native_history"] = "native_history"
    recorded_at: str


class NativeSessionKey(BaseModel):
    """Resolved native identity. A planned ID, cwd, or filename is not a key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    harness: str
    store: str
    native_session_id: str

    @model_validator(mode="after")
    def validate_key(self) -> Self:
        if not self.harness.strip() or not self.store.strip() or not self.native_session_id.strip():
            raise ValueError("native key requires harness, resolved store, and native ID")
        if (
            self.harness != self.harness.strip().lower()
            or self.native_session_id != self.native_session_id.strip()
        ):
            raise ValueError("native harness and ID must use normalized values")
        if self.store != self.store.strip():
            raise ValueError("native store locator must be normalized by its resolver")
        if self.store.startswith("namespace:v1://"):
            parsed = urlsplit(self.store.removeprefix("namespace:v1:"))
            if (
                not parsed.netloc
                or not parsed.path.startswith("/")
                or parsed.query
                or parsed.fragment
                or any(part in {"", ".", ".."} for part in parsed.path[1:].split("/"))
                or _contains_control(self.store)
                or self.store != f"namespace:v1:{parsed.geturl()}"
            ):
                raise ValueError("native namespace must be a canonical namespace:v1 URI")
        elif not _is_canonical_local_store(self.store):
            raise ValueError("native store must be a canonical absolute path or namespace:v1 URI")
        return self


def _is_canonical_local_store(store: str) -> bool:
    return (
        store.startswith("/")
        and not _contains_control(store)
        and (store == "/" or not store.endswith("/"))
        and "//" not in store
        and all(part not in {".", ".."} for part in store.split("/")[1:])
    )


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in value)


class BoundaryEvidence(BaseModel):
    """Experimental caller assertions, NOT adapter-qualified ownership proof."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_attempt_id: str
    transport_scope_id: str
    order: int = Field(ge=0)
    qualified: Literal[True]
    operation: Literal["fresh", "resume", "fork"]
    source_key: NativeSessionKey | None = None
    before_delivery: bool = False
    terminal: bool = False
    fresh_creation_verified: bool = False
    fork_ancestry_verified: bool = False

    @model_validator(mode="after")
    def validate_ordering_identity(self) -> Self:
        if (
            not self.owner_attempt_id.strip()
            or not self.transport_scope_id.strip()
            or self.owner_attempt_id != self.owner_attempt_id.strip()
            or self.transport_scope_id != self.transport_scope_id.strip()
        ):
            raise ValueError("boundary evidence requires normalized attempt and transport scope")
        return self


class OwnedBoundaryReceipt(BaseModel):
    """Experimental caller-supplied observation; owner integration remains unwired."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    attempt_id: str
    boundary: Literal["entry", "exit"]
    key: NativeSessionKey
    evidence: BoundaryEvidence

    @model_validator(mode="after")
    def validate_boundary_evidence(self) -> Self:
        if not self.run_id.strip() or not self.attempt_id.strip():
            raise ValueError("boundary receipt requires run and attempt ownership")
        if self.evidence.owner_attempt_id != self.attempt_id:
            raise ValueError("receipt is not owned by this attempt")
        if self.boundary == "entry" and not self.evidence.before_delivery:
            raise ValueError("entry evidence must precede delivery")
        if self.boundary == "exit" and not self.evidence.terminal:
            raise ValueError("exit evidence must establish terminality")
        return self


class BeginIntent(BaseModel):
    """Immutable experimental owner context; intent does not certify ownership."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    transport_scope_id: str = Field(min_length=1, max_length=256)
    operation: Literal["fresh", "resume", "fork"]
    requested_source: NativeSessionKey | None = None

    @model_validator(mode="after")
    def normalized_context(self) -> Self:
        for value in (self.run_id, self.attempt_id, self.transport_scope_id):
            if value != value.strip() or _contains_control(value):
                raise ValueError("attempt context requires normalized owner identity")
        return self


class BeginEvent(BeginIntent):
    v: Literal[2] = 2
    event: Literal["native_attempt"] = "native_attempt"
    action: Literal["begin"] = "begin"
    attempt_number: int = Field(ge=1)

    def intent(self) -> BeginIntent:
        return BeginIntent(
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            transport_scope_id=self.transport_scope_id,
            operation=self.operation,
            requested_source=self.requested_source,
        )


class BoundaryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[2] = 2
    event: Literal["native_attempt"] = "native_attempt"
    action: Literal["boundary"] = "boundary"
    run_id: str
    attempt_id: str
    receipt: OwnedBoundaryReceipt
    chat_id: PersistedChatId

    @model_validator(mode="after")
    def matching_envelope(self) -> Self:
        if (self.receipt.run_id, self.receipt.attempt_id) != (self.run_id, self.attempt_id):
            raise ValueError("boundary event owner does not match receipt")
        return self


class Refutation(BaseModel):
    """Bounded contradiction of one named exit, not a second terminal receipt.

    causal_reference names an owner-local observation of the contradiction. Like
    the experimental receipt API this records assertions, not transport proof.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[2] = 2
    event: Literal["native_attempt"] = "native_attempt"
    action: Literal["invalidate_exit"] = "invalidate_exit"
    run_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    transport_scope_id: str = Field(min_length=1, max_length=256)
    target_event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    order: int = Field(ge=0)
    reason: Literal["identity_conflict", "same_boundary_conflict", "finality_refuted"]
    conflicting_key: NativeSessionKey | None = None
    causal_reference: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def bounded_evidence(self) -> Self:
        if not self.causal_reference.strip() or _contains_control(self.causal_reference):
            raise ValueError("refutation requires a bounded causal reference")
        if self.conflicting_key is not None and len(self.conflicting_key.model_dump_json()) > 4096:
            raise ValueError("refutation identity evidence exceeds bounded summary")
        return self


type SessionAttemptEvent = BeginEvent | BoundaryEvent | Refutation
type AttemptFact = BeginIntent | OwnedBoundaryReceipt | Refutation
_ATTEMPT_SCHEMA = TypeAdapter[SessionAttemptEvent](
    Annotated[SessionAttemptEvent, Field(discriminator="action")]
)


class BoundaryAcceptance(NamedTuple):
    chat_id: str | None
    invalidated: bool = False


class AttemptBoundaries(NamedTuple):
    entry_chat_id: str | None
    exit_chat_id: str | None
    exit_invalidated: bool


type SessionEvent = (
    SessionStartEvent
    | SessionStopEvent
    | SessionUpdateEvent
    | SessionHistoricalEvent
    | SessionModelSelectionEvent
)


def _record_from_start_event(event: SessionStartEvent) -> SessionRecord:
    return SessionRecord(
        chat_id=event.chat_id,
        history_id=event.history_id,
        kind=event.kind,
        harness=event.harness,
        harness_session_id=event.harness_session_id,
        control_root=event.control_root,
        task_cwd=event.task_cwd,
        execution_cwd=event.execution_cwd,
        claude_config_dir=event.claude_config_dir,
        harness_session_ids=(
            (event.harness_session_id,) if event.harness_session_id is not None else ()
        ),
        model=event.model,
        agent=event.agent,
        agent_path=event.agent_path,
        skills=event.skills,
        skill_paths=event.skill_paths,
        params=event.params,
        started_at=event.started_at,
        stopped_at=None,
        session_instance_id=event.session_instance_id,
        active_work_id=None,
        forked_from_chat_id=event.forked_from_chat_id,
        forked_from_history_id=event.forked_from_history_id,
        spawn_id=event.spawn_id,
    )


def _normalized_generation(generation: str) -> str:
    return generation.strip()


def _generation_matches(expected: str, actual: str) -> bool:
    normalized_expected = _normalized_generation(expected)
    normalized_actual = _normalized_generation(actual)
    if not normalized_expected and not normalized_actual:
        return True
    return normalized_expected == normalized_actual


def project_session_event(
    records: dict[str, SessionRecord], event: SessionEvent, *, collect_harness_ids: bool = True
) -> None:
    """Apply one authoritative event; shared by replay and incremental discovery."""
    if isinstance(event, SessionHistoricalEvent):
        if event.record.record_mode != "historical" or event.record.stopped_at is None:
            raise ValueError("Historical imports must be inactive")
        records[event.chat_id] = event.record
        return
    if isinstance(event, SessionModelSelectionEvent):
        return
    if isinstance(event, SessionStartEvent):
        record = _record_from_start_event(event)
        records[record.chat_id] = record
        return
    existing = records.get(event.chat_id)
    if (
        existing is not None
        and existing.record_mode == "historical"
        and _generation_matches(existing.session_instance_id, event.session_instance_id)
    ):
        raise ValueError("Historical session authority contains a mutation")
    if isinstance(event, SessionStopEvent):
        existing = records.get(event.chat_id)
        if existing is None:
            return
        if not _generation_matches(existing.session_instance_id, event.session_instance_id):
            return
        records[event.chat_id] = existing.model_copy(
            update={
                "stopped_at": event.stopped_at
                if event.stopped_at is not None
                else existing.stopped_at,
                "session_instance_id": event.session_instance_id or existing.session_instance_id,
            }
        )
        return
    existing = records.get(event.chat_id)
    if existing is None:
        return
    if not _generation_matches(existing.session_instance_id, event.session_instance_id):
        return
    session_ids = existing.harness_session_ids
    harness_session_id = existing.harness_session_id
    updated_work_id = existing.active_work_id
    claude_config_dir = existing.claude_config_dir
    spawn_id = existing.spawn_id
    session_instance_id = existing.session_instance_id
    if event.harness_session_id is not None:
        if collect_harness_ids and event.harness_session_id not in session_ids:
            session_ids = (*session_ids, event.harness_session_id)
        harness_session_id = event.harness_session_id
    if event.session_instance_id.strip():
        session_instance_id = event.session_instance_id
    if event.active_work_id is not None:
        normalized_work_id = event.active_work_id.strip()
        updated_work_id = normalized_work_id or None
    if event.claude_config_dir is not None:
        normalized_config_dir = event.claude_config_dir.strip()
        claude_config_dir = normalized_config_dir or None
    if event.spawn_id is not None:
        normalized_spawn_id = event.spawn_id.strip()
        spawn_id = normalized_spawn_id or None
    records[event.chat_id] = existing.model_copy(
        update={
            "harness_session_id": harness_session_id,
            "harness_session_ids": session_ids,
            "session_instance_id": session_instance_id,
            "active_work_id": updated_work_id,
            "claude_config_dir": claude_config_dir,
            "spawn_id": spawn_id,
            "history_id": event.history_id or existing.history_id,
        }
    )


type JournalEvent = SessionEvent | SessionModelObservationEvent | SessionAttemptEvent
type NativeKeyTuple = tuple[str, str, str]
type StartupKey = tuple[str, str, str | None]


def native_key_tuple(key: NativeSessionKey) -> NativeKeyTuple:
    return (key.harness, key.store, key.native_session_id)


def canonical_chat_number(ref: str) -> int:
    match = re.fullmatch(r"c([1-9][0-9]*)", ref, flags=re.ASCII)
    return int(match.group(1)) if match else 0


@dataclass(frozen=True)
class ReferenceOnly:
    pass


@dataclass(frozen=True)
class Operational:
    pass


@dataclass(frozen=True)
class Historical:
    record: SessionRecord


type RefState = ReferenceOnly | Operational | Historical


@dataclass(frozen=True)
class IdentityProjection:
    refs: Mapping[ChatId, RefState]
    chat_to_key: Mapping[ChatId, NativeSessionKey]
    key_to_chat: Mapping[NativeKeyTuple, ChatId]
    max_canonical_number: int


@dataclass(frozen=True)
class BindNative:
    key: NativeSessionKey


@dataclass(frozen=True)
class NeedChat:
    pass


@dataclass(frozen=True)
class NoOp:
    pass


@dataclass(frozen=True)
class IdentityDelta:
    chat_id: ChatId | None = None
    owner: RefState | None = None
    key: NativeSessionKey | None = None
    reference: ChatId | None = None


def plan_identity(
    view: IdentityProjection,
    claim: SessionEvent | SessionModelObservationEvent | BindNative,
    *,
    assigned_chat: ChatId | None = None,
) -> IdentityDelta | NeedChat | NoOp:
    """Check the complete identity effect against the prefix, before mutating it.

    The same planner handles proposals and replay. Replay rejects NoOp historical
    rows; a live retry confirms the existing effect without appending a duplicate.
    """
    if isinstance(claim, BindNative):
        owner = view.key_to_chat.get(native_key_tuple(claim.key))
        if owner is not None:
            if assigned_chat is not None and assigned_chat != owner:
                raise ValueError(f"Duplicate native key claims: {owner}, {assigned_chat}")
            return IdentityDelta(chat_id=owner)
        if assigned_chat is None:
            return NeedChat()
        if assigned_chat in view.refs:
            raise ValueError(f"Native claim on occupied chat alias: {assigned_chat}")
        return IdentityDelta(assigned_chat, Operational(), claim.key)
    if isinstance(claim, SessionHistoricalEvent):
        record = claim.record
        if record.record_mode != "historical" or record.stopped_at is None:
            raise ValueError("Historical imports must be inactive")
        prior = view.refs.get(record.chat_id)
        if isinstance(prior, Historical) and prior.record == record:
            return NoOp()
        if prior is not None:
            raise ValueError(f"Historical claim on occupied chat alias: {record.chat_id}")
        return IdentityDelta(
            record.chat_id, Historical(record), reference=record.forked_from_chat_id
        )
    if isinstance(claim, SessionModelObservationEvent):
        return IdentityDelta()
    match claim:
        case (
            SessionStartEvent()
            | SessionStopEvent()
            | SessionUpdateEvent()
            | SessionModelSelectionEvent()
        ):
            if isinstance(view.refs.get(claim.chat_id), Historical):
                action = "started" if isinstance(claim, SessionStartEvent) else "mutated"
                raise ValueError(
                    f"Historical sessions are inert and cannot be {action}: {claim.chat_id}"
                )
            return IdentityDelta(
                claim.chat_id,
                Operational(),
                reference=claim.forked_from_chat_id
                if isinstance(claim, SessionStartEvent)
                else None,
            )

    assert_never(claim)


@dataclass(frozen=True)
class AttemptState:
    begin: BeginEvent
    entry: BoundaryEvent | None = None
    exit: BoundaryEvent | None = None
    invalidation: Refutation | None = None


@dataclass(frozen=True)
class AttemptProjection:
    states: Mapping[tuple[str, str], AttemptState]
    latest: Mapping[str, BeginEvent]
    effective_exits: int


@dataclass(frozen=True)
class AttemptTransition:
    state: AttemptState
    row: SessionAttemptEvent | None
    result: BoundaryAcceptance = field(default_factory=lambda: BoundaryAcceptance(None))
    identity: IdentityDelta = IdentityDelta()
    exit_delta: int = 0


def plan_attempt(
    attempts: AttemptProjection,
    identity: IdentityProjection,
    fact: AttemptFact,
    *,
    assigned_chat: ChatId | None = None,
) -> AttemptTransition | NeedChat:
    """One attempt policy for proposals and replay; no mutation or prefix copies.

    Allocation is a handshake: NeedChat repeats only this fact with an available
    alias. Strict replay supplies the recorded alias and compares the entire row.
    """
    state = attempts.states.get((fact.run_id, fact.attempt_id))
    latest = attempts.latest.get(fact.run_id)
    if isinstance(fact, BeginIntent):
        if fact.operation == "fresh":
            if fact.requested_source is not None:
                raise ValueError("fresh intent forbids a requested source")
        elif (
            fact.requested_source is None
            or native_key_tuple(fact.requested_source) not in identity.key_to_chat
        ):
            raise ValueError("resume/fork intent requires its pinned source key")
        if state is not None:
            if state.begin.intent() != fact:
                raise ValueError("attempt begin context is immutable")
            return AttemptTransition(state, None)
        event = BeginEvent(
            **fact.model_dump(), attempt_number=1 + (latest.attempt_number if latest else 0)
        )
        return AttemptTransition(AttemptState(event), event)
    if state is None:
        raise ValueError("receipt belongs to an unknown or unstarted attempt")
    begin = state.begin
    scope = (
        fact.transport_scope_id
        if isinstance(fact, Refutation)
        else fact.evidence.transport_scope_id
    )
    if scope != begin.transport_scope_id:
        raise ValueError("observation is not owned by this transport attempt")
    if isinstance(fact, Refutation):
        accepted = state.exit
        if accepted is None or fact.target_event_id != receipt_digest(accepted.receipt):
            raise ValueError("refutation does not name this attempt's accepted exit")
        order = accepted.receipt.evidence.order
        if fact.reason == "finality_refuted":
            if fact.order < order:
                raise ValueError("finality refutation must causally follow its named exit")
        elif (
            fact.conflicting_key is None
            or fact.conflicting_key == accepted.receipt.key
            or (fact.reason == "same_boundary_conflict" and fact.order != order)
            or (fact.reason == "identity_conflict" and fact.order <= order)
        ):
            raise ValueError("identity refutation requires a different key at equal/later order")
        if state.invalidation is not None:
            return AttemptTransition(state, None, BoundaryAcceptance(None, True))
        return AttemptTransition(
            AttemptState(begin, state.entry, accepted, fact),
            fact,
            BoundaryAcceptance(None, True),
            exit_delta=-1,
        )
    evidence = fact.evidence
    if evidence.operation != begin.operation or evidence.source_key != begin.requested_source:
        raise ValueError("observation operation/source differs from immutable attempt intent")
    if fact.boundary == "entry":
        if begin.operation == "resume" and fact.key != begin.requested_source:
            raise ValueError("resume entry does not match its pinned native source")
        if begin.operation == "fork" and (
            fact.key == begin.requested_source or not evidence.fork_ancestry_verified
        ):
            raise ValueError("fork entry lacks distinct target and pinned source evidence")
        if begin.operation == "fresh" and not evidence.fresh_creation_verified:
            raise ValueError("fresh entry lacks fresh-target evidence")
        if state.entry is not None:
            if state.entry.receipt == fact:
                return AttemptTransition(state, None, BoundaryAcceptance(state.entry.chat_id))
            raise ValueError("attempt entry identity is immutable")
        if state.exit is not None:
            raise ValueError("entry cannot be assigned retroactively after exit")
    else:
        if state.entry is not None and evidence.order <= state.entry.receipt.evidence.order:
            raise ValueError("terminal boundary must follow accepted entry ordering evidence")
        if state.invalidation is not None:
            return AttemptTransition(state, None, BoundaryAcceptance(None, True))
        if state.exit is not None:
            prior = state.exit.receipt
            if fact == prior or (fact.key == prior.key and evidence.order > prior.evidence.order):
                return AttemptTransition(state, None, BoundaryAcceptance(state.exit.chat_id))
            if evidence.order < prior.evidence.order:
                raise ValueError("contradictory receipt predates accepted exit")
            if fact.key == prior.key:
                raise ValueError("changed same-key receipt at equal order is not a confirmation")
            # Normalize terminal contradictions into the very same Refutation path.
            return plan_attempt(
                attempts,
                identity,
                Refutation(
                    run_id=fact.run_id,
                    attempt_id=fact.attempt_id,
                    transport_scope_id=evidence.transport_scope_id,
                    target_event_id=receipt_digest(prior),
                    order=evidence.order,
                    reason="same_boundary_conflict"
                    if evidence.order == prior.evidence.order
                    else "identity_conflict",
                    conflicting_key=fact.key,
                    causal_reference=receipt_digest(fact),
                ),
            )
    if latest is None or latest.attempt_id != fact.attempt_id:
        raise ValueError("receipt belongs to a superseded attempt")
    binding = plan_identity(identity, BindNative(fact.key), assigned_chat=assigned_chat)
    if isinstance(binding, NeedChat):
        return binding
    assert isinstance(binding, IdentityDelta) and binding.chat_id is not None
    row = BoundaryEvent(
        run_id=fact.run_id, attempt_id=fact.attempt_id, receipt=fact, chat_id=binding.chat_id
    )
    next_state = AttemptState(
        begin,
        row if fact.boundary == "entry" else state.entry,
        row if fact.boundary == "exit" else state.exit,
        state.invalidation,
    )
    return AttemptTransition(
        next_state,
        row,
        BoundaryAcceptance(binding.chat_id),
        binding,
        int(fact.boundary == "exit"),
    )


@dataclass(frozen=True)
class MetadataProjection:
    starts: Mapping[tuple[str, str, str], SessionStartEvent]
    startup_ids: Mapping[StartupKey, frozenset[str]]
    update_ids: Mapping[StartupKey, frozenset[str]]
    selections: frozenset[tuple[str, str]]
    invocations: frozenset[tuple[str, str, str | None]]
    startup_selections: frozenset[tuple[str, str | None, str, str, str | None]]
    observations: Mapping[tuple[str, str], str]

    def validate_startup(self, event: SessionUpdateEvent | SessionModelSelectionEvent) -> None:
        if event.startup_attempt_id is None or event.harness_session_id is None:
            return
        prior = self.startup_ids.get(startup_key(event), frozenset())
        if prior - {event.harness_session_id}:
            raise ValueError("startup attempt changed its native conversation identity")

    def selection_native_id(self, event: SessionModelSelectionEvent) -> str | None:
        if event.harness_session_id is not None:
            return event.harness_session_id
        ids = self.update_ids.get(startup_key(event), frozenset())
        return next(iter(ids)) if len(ids) == 1 else None


@dataclass(frozen=True)
class JournalSnapshot:
    identity: IdentityProjection
    lifecycle: Mapping[str, SessionRecord]
    metadata: MetadataProjection
    attempts: AttemptProjection


@dataclass(frozen=True)
class JournalRead:
    snapshot: JournalSnapshot
    valid_prefix_end: int
    tail: Literal["empty", "complete_without_delimiter", "torn"]


def startup_key(event: SessionUpdateEvent | SessionModelSelectionEvent) -> StartupKey:
    return (event.chat_id, event.session_instance_id, event.startup_attempt_id)


def selection_startup_key(
    event: SessionModelSelectionEvent,
) -> tuple[str, str | None, str, str, str | None]:
    return (event.harness, event.spawn_id, *startup_key(event))


@dataclass
class _MetadataBuilder:
    starts: dict[tuple[str, str, str], SessionStartEvent] = field(default_factory=dict)
    startup_ids: dict[StartupKey, set[str]] = field(default_factory=dict)
    update_ids: dict[StartupKey, set[str]] = field(default_factory=dict)
    pending: dict[StartupKey, list[SessionModelSelectionEvent]] = field(default_factory=dict)
    selections: dict[tuple[str, str], int] = field(default_factory=dict)
    invocations: dict[tuple[str, str, str | None], int] = field(default_factory=dict)
    startup_selections: set[tuple[str, str | None, str, str, str | None]] = field(
        default_factory=set
    )
    observations: dict[tuple[str, str], str] = field(default_factory=dict)

    def index_selection(self, event: SessionModelSelectionEvent, native: str, delta: int) -> None:
        key = (event.harness, native)
        self.selections[key] = self.selections.get(key, 0) + delta
        if event.kind == "invocation_started":
            invocation = (*key, event.spawn_id)
            self.invocations[invocation] = self.invocations.get(invocation, 0) + delta

    def fold(self, event: JournalEvent) -> None:
        if isinstance(event, SessionStartEvent):
            self.starts.setdefault((event.chat_id, event.session_instance_id, event.harness), event)
        if isinstance(event, SessionModelObservationEvent):
            self.observations[(event.harness, event.harness_session_id)] = (
                event.observed_model_token
            )
        if isinstance(event, (SessionUpdateEvent, SessionModelSelectionEvent)):
            key = startup_key(event)
            native = event.harness_session_id
            if native is not None and event.startup_attempt_id is not None:
                self.startup_ids.setdefault(key, set()).add(native)
            if isinstance(event, SessionUpdateEvent) and (
                native is not None and event.startup_attempt_id is not None
            ):
                ids = self.update_ids.setdefault(key, set())
                # A deferred selection binds on the first update, unbinds on a
                # contradictory second ID, and cannot bind again. Each pending
                # selection is visited at most twice, regardless of journal size.
                if native not in ids:
                    if len(ids) == 1:
                        old = next(iter(ids))
                        for selection in self.pending.pop(key, ()):
                            self.index_selection(selection, old, -1)
                    elif not ids:
                        for selection in self.pending.get(key, ()):
                            self.index_selection(selection, native, 1)
                    ids.add(native)
            if isinstance(event, SessionModelSelectionEvent):
                if event.kind == "invocation_started":
                    self.startup_selections.add(selection_startup_key(event))
                if native is None and event.startup_attempt_id is not None:
                    ids = self.update_ids.get(key, set())
                    if len(ids) <= 1:
                        self.pending.setdefault(key, []).append(event)
                    if len(ids) == 1:
                        native = next(iter(ids))
                if native is not None:
                    self.index_selection(event, native, 1)

    def snapshot(self) -> MetadataProjection:
        return MetadataProjection(
            MappingProxyType(self.starts),
            MappingProxyType({k: frozenset(v) for k, v in self.startup_ids.items()}),
            MappingProxyType({k: frozenset(v) for k, v in self.update_ids.items()}),
            frozenset(k for k, count in self.selections.items() if count),
            frozenset(k for k, count in self.invocations.items() if count),
            frozenset(self.startup_selections),
            MappingProxyType(self.observations),
        )


@dataclass
class _JournalBuilder:
    refs: dict[ChatId, RefState] = field(default_factory=dict)
    chat_to_key: dict[ChatId, NativeSessionKey] = field(default_factory=dict)
    key_to_chat: dict[NativeKeyTuple, ChatId] = field(default_factory=dict)
    max_canonical_number: int = 0
    lifecycle: dict[str, SessionRecord] = field(default_factory=dict)
    lifecycle_ids: dict[str, dict[HarnessSessionId, None]] = field(default_factory=dict)
    metadata: _MetadataBuilder = field(default_factory=_MetadataBuilder)
    attempts: dict[tuple[str, str], AttemptState] = field(default_factory=dict)
    latest: dict[str, BeginEvent] = field(default_factory=dict)
    effective_exits: int = 0

    def identity(self) -> IdentityProjection:
        return IdentityProjection(
            MappingProxyType(self.refs),
            MappingProxyType(self.chat_to_key),
            MappingProxyType(self.key_to_chat),
            self.max_canonical_number,
        )

    def note_ref(self, ref: ChatId, state: RefState) -> None:
        if ref not in self.refs:
            self.max_canonical_number = max(self.max_canonical_number, canonical_chat_number(ref))
        self.refs[ref] = state

    def apply_identity(self, delta: IdentityDelta) -> None:
        if delta.chat_id is not None and delta.owner is not None:
            self.note_ref(delta.chat_id, delta.owner)
        if delta.key is not None:
            assert delta.chat_id is not None
            self.chat_to_key[delta.chat_id] = delta.key
            self.key_to_chat[native_key_tuple(delta.key)] = delta.chat_id
        if delta.reference is not None and delta.reference not in self.refs:
            self.note_ref(delta.reference, ReferenceOnly())

    def attempt_view(self) -> AttemptProjection:
        return AttemptProjection(
            MappingProxyType(self.attempts), MappingProxyType(self.latest), self.effective_exits
        )

    def fold_lifecycle(self, event: SessionEvent) -> None:
        if isinstance(event, SessionStartEvent):
            self.lifecycle_ids[event.chat_id] = (
                {event.harness_session_id: None} if event.harness_session_id is not None else {}
            )
        elif isinstance(event, SessionHistoricalEvent):
            self.lifecycle_ids[event.chat_id] = dict.fromkeys(event.record.harness_session_ids)
        elif isinstance(event, SessionUpdateEvent) and event.harness_session_id is not None:
            prior = self.lifecycle.get(event.chat_id)
            if prior is not None and _generation_matches(
                prior.session_instance_id, event.session_instance_id
            ):
                self.lifecycle_ids[event.chat_id][event.harness_session_id] = None
        # Accumulate native display IDs in an insertion-ordered set, not a
        # repeatedly copied tuple. Materialize the frozen tuple only at the end.
        project_session_event(self.lifecycle, event, collect_harness_ids=False)

    def snapshot(self) -> JournalSnapshot:
        return JournalSnapshot(
            self.identity(),
            MappingProxyType(
                {
                    chat: record
                    if record.record_mode == "historical"
                    else record.model_copy(
                        update={"harness_session_ids": tuple(self.lifecycle_ids[chat])}
                    )
                    for chat, record in self.lifecycle.items()
                }
            ),
            self.metadata.snapshot(),
            AttemptProjection(
                MappingProxyType(self.attempts), MappingProxyType(self.latest), self.effective_exits
            ),
        )


def receipt_digest(receipt: OwnedBoundaryReceipt) -> str:
    return hashlib.sha256(receipt.model_dump_json().encode()).hexdigest()


def fold_row(builder: _JournalBuilder, event: JournalEvent) -> None:
    if isinstance(event, (BeginEvent, BoundaryEvent, Refutation)):
        fact = (
            event.intent()
            if isinstance(event, BeginEvent)
            else (event.receipt if isinstance(event, BoundaryEvent) else event)
        )
        transition = plan_attempt(
            builder.attempt_view(),
            builder.identity(),
            fact,
            assigned_chat=event.chat_id if isinstance(event, BoundaryEvent) else None,
        )
        # A no-op is valid for live retry, never as a persisted duplicate row.
        if isinstance(transition, NeedChat) or transition.row != event:
            raise ValueError("Noncanonical or duplicate native attempt row/assignment")
        builder.attempts[(event.run_id, event.attempt_id)] = transition.state
        if isinstance(event, BeginEvent):
            builder.latest[event.run_id] = event
        builder.effective_exits += transition.exit_delta
        delta = transition.identity
    else:
        delta = plan_identity(builder.identity(), event)
        if isinstance(delta, NoOp):
            assert isinstance(event, SessionHistoricalEvent)
            raise ValueError(f"Duplicate historical import: {event.chat_id}")
        assert isinstance(delta, IdentityDelta)
        if not isinstance(event, SessionModelObservationEvent):
            builder.fold_lifecycle(event)
    # Validate the whole row before applying its effect. No copy of prefix maps.
    builder.apply_identity(delta)
    builder.metadata.fold(event)


_EVENT_SCHEMAS: dict[str, type[JournalEvent]] = {
    "start": SessionStartEvent,
    "stop": SessionStopEvent,
    "update": SessionUpdateEvent,
    "historical_import": SessionHistoricalEvent,
    "model_selection": SessionModelSelectionEvent,
    "model_observation": SessionModelObservationEvent,
}


def decode_row(payload: object) -> JournalEvent:
    """Exhaustive recognized schema boundary; normalize persisted refs once."""
    if not isinstance(payload, dict):
        raise ValueError("Complete sessions row must be a JSON object")
    payload = cast("dict[str, object]", payload)
    kind = payload.get("event")
    if kind == "native_attempt":
        if type(payload.get("v")) is not int or payload.get("v") != 2:
            raise ValueError(
                "Unsupported/frozen native_attempt version requires explicit reconciliation"
            )
        return _ATTEMPT_SCHEMA.validate_python(payload)
    if not isinstance(kind, str) or kind not in _EVENT_SCHEMAS:
        raise ValueError("Unsupported sessions.jsonl event type")
    if kind != "historical_import" and (
        type(payload.get("v", 1)) is not int or payload.get("v", 1) != 1
    ):
        raise ValueError(f"Unsupported {kind} sessions.jsonl version")
    return _EVENT_SCHEMAS[kind].model_validate(payload)


def read_journal(raw: bytes) -> JournalRead:
    builder = _JournalBuilder()
    offset = 0
    tail: Literal["empty", "complete_without_delimiter", "torn"] = "empty"
    lines = raw.split(b"\n")
    for index, line in enumerate(lines):
        terminated = index < len(lines) - 1
        if not line and not terminated:
            break
        if terminated and not line.strip():
            offset += len(line) + 1
            continue
        try:
            payload: object = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if terminated:
                raise ValueError(f"Corrupt sessions.jsonl row {index + 1}") from exc
            if builder.effective_exits:
                raise ValueError("Torn sessions tail may conceal an exit invalidation") from None
            tail = "torn"
            break
        try:
            fold_row(builder, decode_row(payload))
        except ValueError as exc:
            raise ValueError(f"Invalid sessions.jsonl row {index + 1}: {exc}") from exc
        offset += len(line) + int(terminated)
        if not terminated:
            tail = "complete_without_delimiter"
    return JournalRead(builder.snapshot(), offset, tail)
