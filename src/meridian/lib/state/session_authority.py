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


# These are normalized persistence facts, not ownership capabilities. Only the
# bound coordinator submits them; replay validates consistency, not transport truth.
BoundedCorrelation = Annotated[str, Field(min_length=1, max_length=256)]


class CreatedSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Literal["fresh"] = "fresh"
    creation_request: BoundedCorrelation


class ResumeSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Literal["resume"] = "resume"
    source: NativeSessionKey


class ForkSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Literal["fork"] = "fork"
    source: NativeSessionKey
    ancestry_request: BoundedCorrelation


type Selection = Annotated[
    CreatedSelection | ResumeSelection | ForkSelection, Field(discriminator="operation")
]


class BoundaryEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    transport_scope_id: BoundedCorrelation
    order: int = Field(ge=0)
    correlation: BoundedCorrelation
    selection: Selection | None = None
    terminal_rule: BoundedCorrelation | None = None


class BoundaryFact(BaseModel):
    """Internal recorded fact; never accepted from an external caller as proof."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: BoundedCorrelation
    attempt_id: BoundedCorrelation
    boundary: Literal["entry", "exit"]
    key: NativeSessionKey
    evidence: BoundaryEvidence

    @model_validator(mode="after")
    def validate_boundary_evidence(self) -> Self:
        if self.boundary == "entry":
            if self.evidence.selection is None or self.evidence.terminal_rule is not None:
                raise ValueError("entry requires operation evidence, not terminal evidence")
        elif self.evidence.terminal_rule is None or self.evidence.selection is not None:
            raise ValueError("exit requires terminal qualification, not entry evidence")
        return self


# V4 is deliberately a separate frozen wire schema.  Do not add defaults to
# the v3 facts above: their JSON serialization is also their persisted digest.
class LocalObjectStamp(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device: int = Field(ge=0, strict=True)
    inode: int = Field(ge=0, strict=True)


class PendingLocalFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["local_file_pending"]
    path: str
    store_object: LocalObjectStamp

    @model_validator(mode="after")
    def valid_path(self) -> Self:
        _validate_local_observation_path(self.path)
        return self


class QualifiedLocalFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["local_file"]
    path: str
    store_object: LocalObjectStamp
    file_object: LocalObjectStamp
    rule: BoundedCorrelation

    @model_validator(mode="after")
    def valid_path(self) -> Self:
        _validate_local_observation_path(self.path)
        return self


class NoFileObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["no_file_observation"]
    reason: Literal["not_reported", "unsupported_locator"]


type NativeFileObservation = Annotated[
    PendingLocalFile | QualifiedLocalFile | NoFileObservation,
    Field(discriminator="kind"),
]


def _validate_local_observation_path(path: str) -> None:
    if (
        not path.startswith("/")
        or len(path.encode("utf-8")) > 4096
        or _contains_control(path)
        or any(part in {".", ".."} for part in path.split("/"))
        or "//" in path
        or path.endswith("/")
    ):
        raise ValueError("local file observation requires a bounded absolute path")


class BoundaryFactV4(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: BoundedCorrelation
    attempt_id: BoundedCorrelation
    boundary: Literal["entry", "exit"]
    key: NativeSessionKey
    evidence: BoundaryEvidence
    file: NativeFileObservation

    @model_validator(mode="after")
    def validate_fact(self) -> Self:
        if self.boundary == "entry":
            if self.evidence.selection is None or self.evidence.terminal_rule is not None:
                raise ValueError("entry requires operation evidence, not terminal evidence")
        elif self.evidence.terminal_rule is None or self.evidence.selection is not None:
            raise ValueError("exit requires terminal qualification, not entry evidence")
        if isinstance(self.file, (PendingLocalFile, QualifiedLocalFile)):
            if self.key.store.startswith("namespace:v1://") or not self.key.store.startswith("/"):
                raise ValueError("local file observation requires a local store")
            root = self.key.store.rstrip("/")
            if not self.file.path.startswith(root + "/"):
                raise ValueError("local file observation must be below its store")
        return self


class BeginIntentV4(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: BoundedCorrelation
    attempt_id: BoundedCorrelation
    transport_scope_id: BoundedCorrelation
    harness: str
    store: str
    operation: Literal["fresh", "resume", "fork"]
    requested_source: "RecordedNativeSource | None" = None

    @model_validator(mode="after")
    def context(self) -> Self:
        NativeSessionKey(harness=self.harness, store=self.store, native_session_id="context")
        if self.requested_source is not None and (
            self.requested_source.key.harness != self.harness
            or self.requested_source.key.store != self.store
        ):
            raise ValueError("requested source differs from acquired harness/store")
        for value in (self.run_id, self.attempt_id, self.transport_scope_id):
            if value != value.strip() or _contains_control(value):
                raise ValueError("attempt context requires normalized owner identity")
        return self


class BeginEventV4(BeginIntentV4):
    v: Literal[4] = 4
    event: Literal["native_attempt"] = "native_attempt"
    action: Literal["begin"] = "begin"
    attempt_number: int = Field(ge=1)

    def intent(self) -> BeginIntentV4:
        return BeginIntentV4(**self.model_dump(exclude={"v", "event", "action", "attempt_number"}))


class BoundaryEventV4(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    v: Literal[4] = 4
    event: Literal["native_attempt"] = "native_attempt"
    action: Literal["boundary"] = "boundary"
    run_id: BoundedCorrelation
    attempt_id: BoundedCorrelation
    fact: BoundaryFactV4
    chat_id: PersistedChatId

    @model_validator(mode="after")
    def envelope(self) -> Self:
        if (self.fact.run_id, self.fact.attempt_id) != (self.run_id, self.attempt_id):
            raise ValueError("boundary event owner does not match boundary fact")
        return self


class LocatorConflictEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    v: Literal[4] = 4
    event: Literal["native_attempt"] = "native_attempt"
    action: Literal["locator_conflict"] = "locator_conflict"
    run_id: BoundedCorrelation
    attempt_id: BoundedCorrelation
    fact: BoundaryFactV4
    chat_id: PersistedChatId
    binding_event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    store_event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator_event_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: Literal["different_file", "store_replaced"]

    @model_validator(mode="after")
    def target_shape(self) -> Self:
        if (self.fact.run_id, self.fact.attempt_id) != (self.run_id, self.attempt_id):
            raise ValueError("conflict envelope does not match triggering fact")
        if self.reason == "different_file" and self.locator_event_id is None:
            raise ValueError("conflict locator reference does not match reason")
        return self


class NativeSourceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    chat_id: PersistedChatId
    binding_event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator_event_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecordedNativeSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ref: NativeSourceRef
    key: NativeSessionKey
    locator: QualifiedLocalFile


class AcquiredStoreGuard(NamedTuple):
    object: LocalObjectStamp
    store_event_id: str


class NativeBinding(NamedTuple):
    chat_id: ChatId
    key: NativeSessionKey
    binding_event_id: str
    protocol: Literal["v3", "v4"]
    store_guard: AcquiredStoreGuard | None = None
    source_state: Literal["locator_unrecorded", "unobserved", "pending", "pinned"] = "unobserved"
    locator: QualifiedLocalFile | None = None
    locator_event_id: str | None = None
    conflict: LocatorConflictEvent | None = None


class V4AttemptState(NamedTuple):
    begin: BeginEventV4
    entry: BoundaryEventV4 | None = None
    exit: BoundaryEventV4 | None = None
    invalidation: "Refutation | None" = None


class V4Transition(NamedTuple):
    state: V4AttemptState
    row: BeginEventV4 | BoundaryEventV4 | LocatorConflictEvent | None
    chat_id: ChatId | None = None
    binding: NativeBinding | None = None
    conflict: bool = False


type V4AttemptEvent = BeginEventV4 | BoundaryEventV4 | LocatorConflictEvent
_V4_ATTEMPT_SCHEMA = TypeAdapter[V4AttemptEvent](
    Annotated[V4AttemptEvent, Field(discriminator="action")]
)


class BeginIntent(BaseModel):
    """Immutable owner context; intent does not certify ownership."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    transport_scope_id: str = Field(min_length=1, max_length=256)
    harness: str
    store: str
    operation: Literal["fresh", "resume", "fork"]
    requested_source: NativeSessionKey | None = None

    @model_validator(mode="after")
    def normalized_context(self) -> Self:
        for value in (self.run_id, self.attempt_id, self.transport_scope_id):
            if value != value.strip() or _contains_control(value):
                raise ValueError("attempt context requires normalized owner identity")
        NativeSessionKey(harness=self.harness, store=self.store, native_session_id="context")
        if self.requested_source is not None and (
            self.requested_source.harness != self.harness
            or self.requested_source.store != self.store
        ):
            raise ValueError("requested source differs from acquired harness/store")
        return self


class BeginEvent(BeginIntent):
    v: Literal[3] = 3
    event: Literal["native_attempt"] = "native_attempt"
    action: Literal["begin"] = "begin"
    attempt_number: int = Field(ge=1)

    def intent(self) -> BeginIntent:
        return BeginIntent(
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            transport_scope_id=self.transport_scope_id,
            harness=self.harness,
            store=self.store,
            operation=self.operation,
            requested_source=self.requested_source,
        )


class BoundaryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[3] = 3
    event: Literal["native_attempt"] = "native_attempt"
    action: Literal["boundary"] = "boundary"
    run_id: str
    attempt_id: str
    fact: BoundaryFact
    chat_id: PersistedChatId

    @model_validator(mode="after")
    def matching_envelope(self) -> Self:
        if (self.fact.run_id, self.fact.attempt_id) != (self.run_id, self.attempt_id):
            raise ValueError("boundary event owner does not match boundary fact")
        return self


class Refutation(BaseModel):
    """Bounded contradiction of one named exit, not a second terminal boundary fact.

    causal_reference names an owner-local observation of the contradiction. Like
    all recorded facts, this is not transport proof.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[3] = 3
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
type AttemptFact = BeginIntent | BoundaryFact | Refutation
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


type JournalEvent = (
    SessionEvent | SessionModelObservationEvent | SessionAttemptEvent | V4AttemptEvent
)
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
    native_bindings: Mapping[NativeKeyTuple, NativeBinding] = field(default_factory=dict)


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
    states: Mapping[tuple[str, str], AttemptState | V4AttemptState]
    latest: Mapping[str, BeginEvent | BeginEventV4]
    effective_exits: int


@dataclass(frozen=True)
class AttemptTransition:
    state: AttemptState
    row: SessionAttemptEvent | None
    result: BoundaryAcceptance = field(default_factory=lambda: BoundaryAcceptance(None))
    identity: IdentityDelta = IdentityDelta()
    exit_delta: int = 0


@dataclass(frozen=True)
class _BoundaryDecision:
    """Version-neutral phase result; wire rows and digests stay versioned."""

    action: Literal[
        "assign", "repeat", "repeat_entry", "invalidated", "confirm", "stale", "changed", "refute"
    ]
    refutation_reason: Literal["same_boundary_conflict", "identity_conflict"] | None = None


def _validate_boundary_owner(
    proposed: BoundaryFact | BoundaryFactV4,
    *,
    owner_scope: str,
    owner_harness: str,
    owner_store: str,
) -> None:
    if proposed.evidence.transport_scope_id != owner_scope:
        raise ValueError("observation is not owned by this transport attempt")
    if proposed.key.harness != owner_harness or proposed.key.store != owner_store:
        raise ValueError("observation differs from acquired harness/store")


def _classify_boundary_phase(
    *,
    owner_scope: str,
    owner_harness: str,
    owner_store: str,
    latest_attempt_id: str | None,
    entry_order: int | None,
    accepted_entry: BoundaryFact | BoundaryFactV4 | None,
    accepted_exit: BoundaryFact | BoundaryFactV4 | None,
    invalidated: bool,
    proposed: BoundaryFact | BoundaryFactV4,
) -> _BoundaryDecision:
    """Classify shared boundary ordering/retry policy before source or allocation.

    Both frozen codecs expose the same phase evidence. Callers retain their typed
    facts so digest and serialization semantics remain protocol-specific. The
    later v4 cutover replaces its boundary branch with this classifier and routes
    its refutation row through _validate_refutation_reason; no wire normalization
    or v4 live writer belongs here.
    """
    evidence = proposed.evidence
    _validate_boundary_owner(
        proposed,
        owner_scope=owner_scope,
        owner_harness=owner_harness,
        owner_store=owner_store,
    )
    if proposed.boundary == "entry":
        if accepted_entry is not None:
            if proposed == accepted_entry:
                return _BoundaryDecision("repeat_entry")
            raise ValueError("attempt entry identity is immutable")
        if accepted_exit is not None:
            raise ValueError("entry cannot be assigned retroactively after exit")
        decision = _BoundaryDecision("assign")
    else:
        if entry_order is not None and evidence.order <= entry_order:
            raise ValueError("terminal boundary must follow accepted entry ordering evidence")
        if invalidated:
            return _BoundaryDecision("invalidated")
        if accepted_exit is None:
            decision = _BoundaryDecision("assign")
        elif proposed == accepted_exit:
            return _BoundaryDecision("repeat")
        else:
            accepted_order = accepted_exit.evidence.order
            if evidence.order < accepted_order:
                return _BoundaryDecision("stale")
            if proposed.key == accepted_exit.key:
                if evidence.order > accepted_order:
                    return _BoundaryDecision("confirm")
                return _BoundaryDecision("changed")
            return _BoundaryDecision(
                "refute",
                "same_boundary_conflict"
                if evidence.order == accepted_order
                else "identity_conflict",
            )
    if decision.action == "assign" and latest_attempt_id != proposed.attempt_id:
        raise ValueError("boundary fact belongs to a superseded attempt")
    return decision


def _validate_refutation_reason(
    *,
    accepted_key: NativeSessionKey,
    accepted_order: int,
    reason: Literal["same_boundary_conflict", "identity_conflict", "finality_refuted"],
    order: int,
    conflicting_key: NativeSessionKey | None,
) -> None:
    """Common causal bounds for explicit and synthesized exit refutations."""
    if reason == "finality_refuted":
        if order < accepted_order:
            raise ValueError("finality refutation must causally follow its named exit")
    elif (
        conflicting_key is None
        or conflicting_key == accepted_key
        or (reason == "same_boundary_conflict" and order != accepted_order)
        or (reason == "identity_conflict" and order <= accepted_order)
    ):
        raise ValueError("identity refutation requires a different key at equal/later order")


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
            if not isinstance(state, AttemptState):
                raise ValueError("native attempt cannot mix v3 and v4 rows")
            if state.begin.intent() != fact:
                raise ValueError("attempt begin context is immutable")
            return AttemptTransition(state, None)
        if latest is not None and not isinstance(latest, BeginEvent):
            raise ValueError("native attempt cannot mix v3 and v4 rows")
        event = BeginEvent(
            **fact.model_dump(), attempt_number=1 + (latest.attempt_number if latest else 0)
        )
        return AttemptTransition(AttemptState(event), event)
    if state is None or not isinstance(state, AttemptState):
        raise ValueError("boundary fact belongs to an unknown or unstarted attempt")
    begin = state.begin
    if isinstance(fact, Refutation):
        if fact.transport_scope_id != begin.transport_scope_id:
            raise ValueError("observation is not owned by this transport attempt")
        accepted = state.exit
        if accepted is None or fact.target_event_id != boundary_digest(accepted.fact):
            raise ValueError("refutation does not name this attempt's accepted exit")
        _validate_refutation_reason(
            accepted_key=accepted.fact.key,
            accepted_order=accepted.fact.evidence.order,
            reason=fact.reason,
            order=fact.order,
            conflicting_key=fact.conflicting_key,
        )
        if state.invalidation is not None:
            return AttemptTransition(state, None, BoundaryAcceptance(None, True))
        return AttemptTransition(
            AttemptState(begin, state.entry, accepted, fact),
            fact,
            BoundaryAcceptance(None, True),
            exit_delta=-1,
        )
    _validate_boundary_owner(
        fact,
        owner_scope=begin.transport_scope_id,
        owner_harness=begin.harness,
        owner_store=begin.store,
    )
    evidence = fact.evidence
    if fact.boundary == "entry":
        selection = evidence.selection
        if (
            selection is None
            or selection.operation != begin.operation
            or (
                isinstance(selection, (ResumeSelection, ForkSelection))
                and selection.source != begin.requested_source
            )
        ):
            raise ValueError("observation operation/source differs from immutable attempt intent")
        if begin.operation == "resume" and fact.key != begin.requested_source:
            raise ValueError("resume entry does not match its pinned native source")
        if begin.operation == "fork" and (
            fact.key == begin.requested_source or not isinstance(selection, ForkSelection)
        ):
            raise ValueError("fork entry lacks distinct target and pinned source evidence")
        if begin.operation == "fresh" and not isinstance(selection, CreatedSelection):
            raise ValueError("fresh entry lacks fresh-target evidence")
    decision = _classify_boundary_phase(
        owner_scope=begin.transport_scope_id,
        owner_harness=begin.harness,
        owner_store=begin.store,
        latest_attempt_id=latest.attempt_id if latest is not None else None,
        entry_order=(state.entry.fact.evidence.order if state.entry is not None else None),
        accepted_entry=state.entry.fact if state.entry is not None else None,
        accepted_exit=state.exit.fact if state.exit is not None else None,
        invalidated=state.invalidation is not None,
        proposed=fact,
    )
    if decision.action == "repeat_entry":
        assert state.entry is not None
        return AttemptTransition(state, None, BoundaryAcceptance(state.entry.chat_id))
    if decision.action == "invalidated":
        return AttemptTransition(state, None, BoundaryAcceptance(None, True))
    if decision.action in ("repeat", "confirm"):
        assert state.exit is not None
        return AttemptTransition(state, None, BoundaryAcceptance(state.exit.chat_id))
    if decision.action == "stale":
        raise ValueError("contradictory boundary fact predates accepted exit")
    if decision.action == "changed":
        raise ValueError("changed same-key boundary fact at equal order is not a confirmation")
    if decision.action == "refute":
        assert state.exit is not None and decision.refutation_reason is not None
        return plan_attempt(
            attempts,
            identity,
            Refutation(
                run_id=fact.run_id,
                attempt_id=fact.attempt_id,
                transport_scope_id=evidence.transport_scope_id,
                target_event_id=boundary_digest(state.exit.fact),
                order=evidence.order,
                reason=decision.refutation_reason,
                conflicting_key=fact.key,
                causal_reference=boundary_digest(fact),
            ),
        )
    binding = plan_identity(identity, BindNative(fact.key), assigned_chat=assigned_chat)
    if isinstance(binding, NeedChat):
        return binding
    assert isinstance(binding, IdentityDelta) and binding.chat_id is not None
    row = BoundaryEvent(
        run_id=fact.run_id, attempt_id=fact.attempt_id, fact=fact, chat_id=binding.chat_id
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


def boundary_digest_v4(fact: BoundaryFactV4) -> str:
    """Version-domain-separated digest; v3 digests remain byte-for-byte frozen."""
    return hashlib.sha256(b"native-attempt-v4\0" + fact.model_dump_json().encode()).hexdigest()


def _same_pin(left: QualifiedLocalFile, right: QualifiedLocalFile) -> bool:
    return (left.path, left.store_object, left.file_object) == (
        right.path,
        right.store_object,
        right.file_object,
    )


def plan_attempt_v4(
    states: Mapping[tuple[str, str], AttemptState | V4AttemptState],
    latest: Mapping[str, BeginEvent | BeginEventV4],
    identity: IdentityProjection,
    fact: BeginIntentV4 | BoundaryFactV4 | LocatorConflictEvent,
    *,
    assigned_chat: ChatId | None = None,
) -> V4Transition | NeedChat:
    """Pure v4 journal transition. File facts are lexical values; this does no I/O."""
    state = states.get((fact.run_id, fact.attempt_id))
    current_latest = latest.get(fact.run_id)
    if isinstance(fact, BeginIntentV4):
        if state is not None and not isinstance(state, V4AttemptState):
            raise ValueError("native attempt cannot mix v3 and v4 rows")
        if current_latest is not None and not isinstance(current_latest, BeginEventV4):
            raise ValueError("native attempt cannot mix v3 and v4 rows")
        if fact.operation == "fresh" and fact.requested_source is not None:
            raise ValueError("fresh intent forbids a requested source")
        if fact.operation != "fresh":
            source = fact.requested_source
            if source is None:
                raise ValueError("resume/fork requires a recorded source")
            binding = identity.native_bindings.get(native_key_tuple(source.key))
            if (
                binding is None
                or binding.protocol != "v4"
                or binding.conflict is not None
                or binding.chat_id != source.ref.chat_id
                or binding.binding_event_id != source.ref.binding_event_id
                or binding.locator_event_id != source.ref.locator_event_id
                or binding.locator != source.locator
                or binding.locator is None
            ):
                raise ValueError("resume/fork requires the current unblocked recorded source")
        if state is not None:
            if state.begin.intent() != fact:
                raise ValueError("attempt begin context is immutable")
            return V4Transition(state, None)
        number = current_latest.attempt_number if current_latest is not None else 0
        event = BeginEventV4(**fact.model_dump(), attempt_number=number + 1)
        return V4Transition(V4AttemptState(event), event)

    if state is None or not isinstance(state, V4AttemptState):
        raise ValueError("v4 boundary belongs to unknown or non-v4 attempt")
    begin = state.begin
    if isinstance(fact, LocatorConflictEvent):
        trigger = fact.fact
        if (
            trigger.evidence.transport_scope_id != begin.transport_scope_id
            or trigger.key.harness != begin.harness
            or trigger.key.store != begin.store
            or current_latest is None
            or current_latest.attempt_id != fact.attempt_id
        ):
            raise ValueError("locator conflict is not owned by the current attempt")
        if trigger.boundary == "entry":
            selection = trigger.evidence.selection
            if (
                selection is None
                or selection.operation != begin.operation
                or begin.operation != "fresh"
                or not isinstance(selection, CreatedSelection)
                or state.entry is not None
                or state.exit is not None
            ):
                raise ValueError("locator conflict entry is not an assignable boundary")
        elif state.entry is not None and (
            trigger.evidence.order <= state.entry.fact.evidence.order
        ):
            raise ValueError("locator conflict exit does not follow the accepted entry")
        if state.exit is not None and (
            trigger.key != state.exit.fact.key
            or trigger.evidence.order < state.exit.fact.evidence.order
        ):
            raise ValueError("locator conflict does not target the accepted exit identity")
        binding = identity.native_bindings.get(native_key_tuple(trigger.key))
        if binding is None or binding.conflict is not None:
            raise ValueError("locator conflict has no unblocked native binding")
        if binding.store_guard is None:
            raise ValueError("locator conflict has no acquired store guard")
        if (
            fact.chat_id != binding.chat_id
            or fact.binding_event_id != binding.binding_event_id
            or fact.store_event_id != binding.store_guard.store_event_id
            or fact.locator_event_id != binding.locator_event_id
        ):
            raise ValueError("locator conflict target does not match binding provenance")
        observation = trigger.file
        if fact.reason == "different_file":
            if (
                binding.locator is None
                or not isinstance(observation, QualifiedLocalFile)
                or observation.store_object != binding.store_guard.object
                or _same_pin(binding.locator, observation)
            ):
                raise ValueError("different-file conflict does not prove a competing pin")
        elif (
            not isinstance(observation, (PendingLocalFile, QualifiedLocalFile))
            or observation.store_object == binding.store_guard.object
        ):
            raise ValueError("store-replaced conflict does not prove a competing store")
        return V4Transition(state, fact, fact.chat_id, binding._replace(conflict=fact), True)

    evidence = fact.evidence
    if (fact.run_id, fact.attempt_id) != (begin.run_id, begin.attempt_id):
        raise ValueError("boundary belongs to a different attempt")
    if evidence.transport_scope_id != begin.transport_scope_id:
        raise ValueError("observation is not owned by this transport attempt")
    if fact.key.harness != begin.harness or fact.key.store != begin.store:
        raise ValueError("observation differs from acquired harness/store")
    if fact.boundary == "entry":
        selection = evidence.selection
        if (
            selection is None
            or selection.operation != begin.operation
            or (
                isinstance(selection, (ResumeSelection, ForkSelection))
                and (
                    begin.requested_source is None or selection.source != begin.requested_source.key
                )
            )
        ):
            raise ValueError("entry operation differs from immutable attempt intent")
        requested = begin.requested_source
        if begin.operation != "fresh" and requested is None:
            raise ValueError("resume/fork entry lacks its recorded source")
        if begin.operation == "resume" and requested is not None and fact.key != requested.key:
            raise ValueError("resume entry does not match its recorded native source")
        if (
            begin.operation == "fork"
            and requested is not None
            and (fact.key == requested.key or not isinstance(selection, ForkSelection))
        ):
            raise ValueError("fork entry lacks a distinct target and recorded source")
        if begin.operation == "fresh" and not isinstance(selection, CreatedSelection):
            raise ValueError("fresh entry lacks fresh-target evidence")
        if state.entry is not None:
            if state.entry.fact == fact:
                return V4Transition(state, None, state.entry.chat_id)
            raise ValueError("attempt entry identity is immutable")
        if state.exit is not None:
            raise ValueError("entry cannot be assigned retroactively after exit")
    elif state.entry is not None and evidence.order <= state.entry.fact.evidence.order:
        raise ValueError("terminal boundary must follow accepted entry ordering evidence")
    if current_latest is None or current_latest.attempt_id != fact.attempt_id:
        raise ValueError("boundary fact belongs to a superseded attempt")

    key_id = native_key_tuple(fact.key)
    existing = identity.native_bindings.get(key_id)
    if existing is not None and existing.conflict is not None:
        raise ValueError("native binding is blocked by a source conflict")
    if existing is not None and existing.protocol != "v4":
        raise ValueError("v3 native bindings require explicit reconciliation")
    chat = identity.key_to_chat.get(key_id)
    if existing is None and chat is not None:
        raise ValueError("legacy or v3 native binding requires explicit reconciliation")
    if existing is None and assigned_chat is None:
        return NeedChat()
    if existing is None and assigned_chat in identity.refs:
        raise ValueError("native claim on occupied chat alias")
    if existing is not None and assigned_chat is not None and assigned_chat != existing.chat_id:
        raise ValueError("native key is already bound to a different chat")
    target_chat = existing.chat_id if existing is not None else assigned_chat
    assert target_chat is not None
    digest = boundary_digest_v4(fact)
    binding = existing or NativeBinding(target_chat, fact.key, digest, "v4")
    observation = fact.file
    if isinstance(observation, (PendingLocalFile, QualifiedLocalFile)):
        if binding.store_guard is None:
            binding = binding._replace(
                store_guard=AcquiredStoreGuard(observation.store_object, digest),
                source_state="pending"
                if isinstance(observation, PendingLocalFile)
                else "unobserved",
            )
        elif observation.store_object != binding.store_guard.object:
            conflict = LocatorConflictEvent(
                run_id=fact.run_id,
                attempt_id=fact.attempt_id,
                fact=fact,
                chat_id=binding.chat_id,
                binding_event_id=binding.binding_event_id,
                store_event_id=binding.store_guard.store_event_id,
                locator_event_id=binding.locator_event_id,
                reason="store_replaced",
            )
            return V4Transition(
                state, conflict, binding.chat_id, binding._replace(conflict=conflict), True
            )
        if isinstance(observation, QualifiedLocalFile):
            if binding.locator is None:
                binding = binding._replace(
                    locator=observation, locator_event_id=digest, source_state="pinned"
                )
            elif not _same_pin(binding.locator, observation):
                assert binding.store_guard is not None
                conflict = LocatorConflictEvent(
                    run_id=fact.run_id,
                    attempt_id=fact.attempt_id,
                    fact=fact,
                    chat_id=binding.chat_id,
                    binding_event_id=binding.binding_event_id,
                    store_event_id=binding.store_guard.store_event_id,
                    locator_event_id=binding.locator_event_id,
                    reason="different_file",
                )
                return V4Transition(
                    state, conflict, binding.chat_id, binding._replace(conflict=conflict), True
                )
        elif binding.locator is None:
            binding = binding._replace(source_state="pending")
    if fact.boundary == "exit" and state.exit is not None:
        previous = state.exit.fact
        if fact == previous or (
            fact.key == previous.key and evidence.order > previous.evidence.order
        ):
            return V4Transition(state, None, state.exit.chat_id)
        if fact.key != previous.key:
            raise ValueError("v4 exit identity contradiction requires explicit refutation")
        raise ValueError("changed same-key boundary fact at equal order is not a confirmation")
    row = BoundaryEventV4(
        run_id=fact.run_id, attempt_id=fact.attempt_id, fact=fact, chat_id=target_chat
    )
    next_state = V4AttemptState(
        begin,
        row if fact.boundary == "entry" else state.entry,
        row if fact.boundary == "exit" else state.exit,
    )
    return V4Transition(next_state, row, target_chat, binding)


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
    native_bindings: dict[NativeKeyTuple, NativeBinding] = field(default_factory=dict)
    max_canonical_number: int = 0
    lifecycle: dict[str, SessionRecord] = field(default_factory=dict)
    lifecycle_ids: dict[str, dict[HarnessSessionId, None]] = field(default_factory=dict)
    metadata: _MetadataBuilder = field(default_factory=_MetadataBuilder)
    attempts: dict[tuple[str, str], AttemptState | V4AttemptState] = field(default_factory=dict)
    latest: dict[str, BeginEvent | BeginEventV4] = field(default_factory=dict)
    effective_exits: int = 0

    def identity(self) -> IdentityProjection:
        return IdentityProjection(
            MappingProxyType(self.refs),
            MappingProxyType(self.chat_to_key),
            MappingProxyType(self.key_to_chat),
            self.max_canonical_number,
            MappingProxyType(self.native_bindings),
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


def boundary_digest(fact: BoundaryFact) -> str:
    return hashlib.sha256(fact.model_dump_json().encode()).hexdigest()


def fold_row(builder: _JournalBuilder, event: JournalEvent) -> None:
    if isinstance(event, (BeginEventV4, BoundaryEventV4, LocatorConflictEvent)):
        fact = event.intent() if isinstance(event, BeginEventV4) else event.fact
        transition = plan_attempt_v4(
            builder.attempts,
            builder.latest,
            builder.identity(),
            fact,
            assigned_chat=event.chat_id if isinstance(event, BoundaryEventV4) else None,
        )
        if isinstance(transition, NeedChat) or transition.row != event:
            raise ValueError("Noncanonical or duplicate v4 native attempt row/assignment")
        builder.attempts[(event.run_id, event.attempt_id)] = transition.state
        if isinstance(event, BeginEventV4):
            builder.latest[event.run_id] = event
        if transition.binding is not None:
            identity = transition.binding
            key_id = native_key_tuple(identity.key)
            builder.native_bindings[key_id] = identity
            if isinstance(event, BoundaryEventV4):
                delta = IdentityDelta(identity.chat_id, Operational(), identity.key)
                builder.apply_identity(delta)
        return
    if isinstance(event, (BeginEvent, BoundaryEvent, Refutation)):
        fact = (
            event.intent()
            if isinstance(event, BeginEvent)
            else (event.fact if isinstance(event, BoundaryEvent) else event)
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
        if isinstance(event, BoundaryEvent):
            key_id = native_key_tuple(event.fact.key)
            if key_id not in builder.native_bindings:
                builder.native_bindings[key_id] = NativeBinding(
                    event.chat_id,
                    event.fact.key,
                    boundary_digest(event.fact),
                    "v3",
                    source_state="locator_unrecorded",
                )
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
        version = payload.get("v")
        if type(version) is not int or version not in (3, 4):
            raise ValueError(
                "Unsupported/frozen native_attempt version requires explicit reconciliation"
            )
        return (_ATTEMPT_SCHEMA if version == 3 else _V4_ATTEMPT_SCHEMA).validate_python(payload)
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
            if builder.effective_exits or builder.native_bindings:
                raise ValueError(
                    "Torn sessions tail may conceal native authority invalidation"
                ) from None
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
