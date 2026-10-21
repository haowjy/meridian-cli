"""Session event models and deterministic authority projections.

Disk reads and lease ownership belong to session_store; replay accepts events.
"""

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.logging import warn_once
from meridian.lib.core.native_identity import BindSource, NativeKey, NativeKeyFields
from meridian.lib.core.types import (
    HarnessSessionId,
    OptionalPersistedChatId,
    OptionalPersistedHarnessSessionId,
    PersistedChatId,
)
from meridian.lib.state.native_binding import Conflict, bind

HISTORICAL_GENERATION_PREFIX = "historical-"
SUPPORTED_SESSION_EVENT_VERSIONS = frozenset({1})
logger = logging.getLogger(__name__)


def _warn_unsupported_session_versions(path: Path, versions: tuple[str, ...]) -> None:
    version_noun = "version" if len(versions) == 1 else "versions"
    warn_once(
        ("session-format", path, versions),
        lambda: logger.warning(
            "Skipping records in %s with unsupported sessions.jsonl format %s %s; "
            "upgrade Meridian to read these records.",
            path,
            version_noun,
            ", ".join(versions),
        ),
    )


class UnsupportedSessionFormat(ValueError):
    """A read-then-decide path found sessions.jsonl rows a newer build wrote."""


@dataclass
class UnsupportedSessionRecords:
    """Collect unsupported versions found during one session-log read."""

    versions: set[str] = field(default_factory=set[str])

    def record(self, version: object) -> None:
        self.versions.add(repr(version))

    def warn(self, path: Path) -> None:
        if not self.versions:
            return
        _warn_unsupported_session_versions(path, tuple(sorted(self.versions)))

    def require_supported(self, path: Path) -> None:
        if not self.versions:
            return
        versions = ", ".join(sorted(self.versions))
        raise UnsupportedSessionFormat(
            f"{path}: unsupported sessions.jsonl format version(s) {versions}; "
            "upgrade Meridian before updating these session records."
        )


def supported_session_event_version(
    payload: Mapping[str, object],
    unsupported: UnsupportedSessionRecords,
) -> bool:
    version = payload.get("v", 1)
    if type(version) is int and version in SUPPORTED_SESSION_EVENT_VERSIONS:
        return True
    unsupported.record(version)
    return False


def normalized_generation(generation: str) -> str:
    return generation.strip()


def new_historical_generation() -> str:
    return f"{HISTORICAL_GENERATION_PREFIX}{uuid.uuid4().hex}"


def is_historical_generation(generation: str) -> bool:
    return normalized_generation(generation).startswith(HISTORICAL_GENERATION_PREFIX)


class SessionSettings(BaseModel):
    """Context inherited by a new conversation, never identity, lineage or a lease."""

    model: str
    agent: str
    agent_path: str
    skills: tuple[str, ...]
    skill_paths: tuple[str, ...]
    params: tuple[str, ...]
    control_root: str | None = None
    task_cwd: str | None = None
    execution_cwd: str | None = None
    claude_config_dir: str | None = None
    active_work_id: str | None = None
    launch_policy_snapshot: LaunchPolicySnapshot | None = None


class SessionRecord(SessionSettings):
    model_config = ConfigDict(frozen=True)

    chat_id: PersistedChatId
    history_id: uuid.UUID | None = None
    record_mode: Literal["live", "historical"] = "live"
    kind: Literal["primary", "spawn"]
    harness: str
    harness_session_id: OptionalPersistedHarnessSessionId
    native_store: str | None = None
    started_at: str
    created_at: str = ""
    stopped_at: str | None
    session_instance_id: str = ""
    forked_from_chat_id: OptionalPersistedChatId = None
    forked_from_history_id: uuid.UUID | None = None
    context_from_chat_id: OptionalPersistedChatId = None
    context_from_history_id: uuid.UUID | None = None
    tmux_target: str | None = None
    spawn_id: str | None = None

    def key_fields(self) -> NativeKeyFields:
        return NativeKeyFields(self.harness, self.native_store, self.harness_session_id)

    def native_key(self) -> NativeKey | None:
        return self.key_fields().complete()


class SessionStartEvent(SessionSettings):
    model_config = ConfigDict(extra="ignore")

    history_id: uuid.UUID | None = None
    v: int = 1
    event: Literal["start"] = "start"
    chat_id: PersistedChatId
    kind: Literal["primary", "spawn"] = "spawn"
    harness: str
    harness_session_id: OptionalPersistedHarnessSessionId
    native_store: str | None = None
    agent: str = ""
    agent_path: str = ""
    skills: tuple[str, ...] = ()
    skill_paths: tuple[str, ...] = ()
    params: tuple[str, ...] = ()
    session_instance_id: str = ""
    started_at: str
    forked_from_chat_id: OptionalPersistedChatId = None
    forked_from_history_id: uuid.UUID | None = None
    context_from_chat_id: OptionalPersistedChatId = None
    context_from_history_id: uuid.UUID | None = None
    tmux_target: str | None = None
    spawn_id: str | None = None
    model_selection_protocol: Literal[1] | None = None

    def key_fields(self) -> NativeKeyFields:
        return NativeKeyFields(self.harness, self.native_store, self.harness_session_id)


class SessionStopEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["stop"] = "stop"
    chat_id: PersistedChatId
    session_instance_id: str = ""
    stopped_at: str | None = None


class SessionUpdateEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["update"] = "update"
    chat_id: PersistedChatId
    harness_session_id: OptionalPersistedHarnessSessionId = None
    session_instance_id: str = ""
    native_store: str | None = None
    claude_config_dir: str | None = None
    active_work_id: str | None = None
    spawn_id: str | None = None
    history_id: uuid.UUID | None = None
    startup_attempt_id: str | None = None
    source: BindSource | None = None
    launch_policy_snapshot: LaunchPolicySnapshot | None = None

    def key_fields(self) -> NativeKeyFields:
        return NativeKeyFields(native_store=self.native_store, session_id=self.harness_session_id)


class SessionHistoricalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["historical_import"] = "historical_import"
    record: SessionRecord

    @property
    def chat_id(self) -> str:
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

    v: int = 1
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

    v: int = 1
    event: Literal["model_observation"] = "model_observation"
    harness: str
    harness_session_id: HarnessSessionId
    observed_model_token: str
    observed_source: Literal["native_history"] = "native_history"
    recorded_at: str


type SessionEvent = (
    SessionStartEvent
    | SessionStopEvent
    | SessionUpdateEvent
    | SessionHistoricalEvent
    | SessionModelSelectionEvent
)


def parse_event(
    payload: Mapping[str, object],
    unsupported: UnsupportedSessionRecords,
) -> SessionEvent | None:
    if not supported_session_event_version(payload, unsupported):
        return None
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
            return SessionModelSelectionEvent.model_validate(payload)
    except ValidationError:
        return None
    return None


def _record_from_start_event(event: SessionStartEvent) -> SessionRecord:
    return SessionRecord(
        chat_id=event.chat_id,
        history_id=event.history_id,
        kind=event.kind,
        harness=event.harness,
        harness_session_id=event.harness_session_id,
        native_store=event.native_store,
        **event.model_dump(include=set(SessionSettings.model_fields)),
        started_at=event.started_at,
        created_at=event.started_at,
        stopped_at=None,
        session_instance_id=event.session_instance_id,
        forked_from_chat_id=event.forked_from_chat_id,
        forked_from_history_id=event.forked_from_history_id,
        context_from_chat_id=event.context_from_chat_id,
        context_from_history_id=event.context_from_history_id,
        tmux_target=event.tmux_target,
        spawn_id=event.spawn_id,
    )


def generation_matches(expected: str, actual: str) -> bool:
    normalized_expected = normalized_generation(expected)
    normalized_actual = normalized_generation(actual)
    if not normalized_expected and not normalized_actual:
        return True
    return normalized_expected == normalized_actual


def validate_historical_import(event: SessionHistoricalEvent) -> None:
    if event.record.record_mode != "historical" or event.record.stopped_at is None:
        raise ValueError("Historical imports must be inactive")


def project_session_event(records: dict[str, SessionRecord], event: SessionEvent) -> bool:
    """Apply one authoritative event; shared by replay and incremental discovery."""
    if isinstance(event, SessionHistoricalEvent):
        validate_historical_import(event)
        records[event.chat_id] = event.record
        return True
    if isinstance(event, SessionModelSelectionEvent):
        return False
    if isinstance(event, SessionStartEvent):
        record = _record_from_start_event(event)
        existing = records.get(event.chat_id)
        if existing is not None:
            outcome = bind(existing.key_fields(), event.key_fields())
            if isinstance(outcome, Conflict):
                return False
            record = record.model_copy(
                update={"created_at": existing.created_at or existing.started_at}
            )
            record = with_key(record, outcome.key)
            for prefix in ("forked_from", "context_from"):
                parent_fields = (f"{prefix}_chat_id", f"{prefix}_history_id")
                if any(getattr(existing, field) is not None for field in parent_fields):
                    record = record.model_copy(
                        update={field: getattr(existing, field) for field in parent_fields}
                    )
        records[record.chat_id] = record
        return True
    existing = records.get(event.chat_id)
    if (
        existing is not None
        and existing.record_mode == "historical"
        and generation_matches(existing.session_instance_id, event.session_instance_id)
    ):
        raise ValueError("Historical session authority contains a mutation")
    if isinstance(event, SessionStopEvent):
        existing = records.get(event.chat_id)
        if existing is None:
            return False
        if not generation_matches(existing.session_instance_id, event.session_instance_id):
            return False
        records[event.chat_id] = existing.model_copy(
            update={
                "stopped_at": event.stopped_at
                if event.stopped_at is not None
                else existing.stopped_at,
                "session_instance_id": event.session_instance_id or existing.session_instance_id,
            }
        )
        return True
    existing = records.get(event.chat_id)
    if existing is None:
        return False
    if not generation_matches(existing.session_instance_id, event.session_instance_id):
        return False
    if isinstance(bind(existing.key_fields(), event.key_fields()), Conflict):
        return False
    harness_session_id = existing.harness_session_id
    updated_work_id = existing.active_work_id
    claude_config_dir = existing.claude_config_dir
    spawn_id = existing.spawn_id
    session_instance_id = existing.session_instance_id
    if event.harness_session_id is not None:
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
            "native_store": existing.native_store or event.native_store,
            "session_instance_id": session_instance_id,
            "active_work_id": updated_work_id,
            "claude_config_dir": claude_config_dir,
            "spawn_id": spawn_id,
            "history_id": event.history_id or existing.history_id,
            "launch_policy_snapshot": event.launch_policy_snapshot
            or existing.launch_policy_snapshot,
        }
    )
    return True


def validate_startup_identity(
    events: list[SessionEvent],
    event: SessionUpdateEvent | SessionModelSelectionEvent,
) -> None:
    if event.startup_attempt_id is None or event.harness_session_id is None:
        return
    for prior in events:
        if isinstance(prior, (SessionUpdateEvent, SessionModelSelectionEvent)) and (
            prior.chat_id == event.chat_id
            and prior.session_instance_id == event.session_instance_id
            and prior.startup_attempt_id == event.startup_attempt_id
            and prior.harness_session_id is not None
            and prior.harness_session_id != event.harness_session_id
        ):
            raise ValueError("startup attempt changed its native conversation identity")


def fold_session_generations(events: Iterable[SessionEvent]) -> tuple[SessionRecord, ...]:
    """Project generations in source order; chat acceptance uses the same fold.

    Bucket keys retain raw generation spelling, unlike generation matching. Late
    updates can still apply to an older bucket if compatible with the chat key.
    """
    generations: dict[tuple[str, str], dict[str, SessionRecord]] = {}
    open_generations: dict[str, set[str]] = {}
    latest_blank: dict[str, str] = {}
    records: dict[str, SessionRecord] = {}
    for ordinal, event in enumerate(events):
        project_session_generation(
            records,
            generations,
            open_generations,
            latest_blank,
            event,
            ordinal=ordinal,
        )
    return tuple(record for rows in generations.values() for record in rows.values())


def _close_abandoned_generations(
    generations: dict[tuple[str, str], dict[str, SessionRecord]],
    open_generations: dict[str, set[str]],
    chat_id: str,
    generation: str,
    stopped_at: str,
) -> tuple[tuple[str, str], ...]:
    """Close prior generations once a later accepted start proves lock release."""
    prior_generations = open_generations.get(chat_id)
    if prior_generations is None:
        return ()
    changed: list[tuple[str, str]] = []
    for prior_generation in sorted(prior_generations):
        if prior_generation == generation:
            continue
        prior_generations.discard(prior_generation)
        key = (chat_id, prior_generation)
        prior_rows = generations.get(key)
        if prior_rows is None:
            continue
        prior = prior_rows.get(chat_id)
        if prior is None or prior.record_mode == "historical" or prior.stopped_at is not None:
            continue
        prior_rows[chat_id] = prior.model_copy(update={"stopped_at": stopped_at})
        changed.append(key)
    if not prior_generations:
        del open_generations[chat_id]
    return tuple(changed)


def project_session_generation(
    records: dict[str, SessionRecord],
    generations: dict[tuple[str, str], dict[str, SessionRecord]],
    open_generations: dict[str, set[str]],
    latest_blank: dict[str, str],
    event: SessionEvent,
    *,
    ordinal: int,
) -> tuple[tuple[str, str], ...]:
    """Advance replay working sets and return every changed generation bucket."""
    chat_id = event.chat_id
    applied = project_session_event(records, event)
    if isinstance(event, SessionStartEvent):
        if not applied:
            return ()
        event = with_key(event, records[chat_id].key_fields())
    elif isinstance(event, SessionUpdateEvent) and not applied:
        existing = records.get(chat_id)
        if existing is not None and isinstance(
            bind(existing.key_fields(), event.key_fields()), Conflict
        ):
            return ()
    generation = event.session_instance_id
    if not generation:
        if isinstance(event, SessionStartEvent):
            latest_blank[chat_id] = f"legacy:{ordinal}"
        generation = latest_blank.get(chat_id, "")
    changed: list[tuple[str, str]] = []
    if isinstance(event, SessionStartEvent):
        # A start is published only after its writer owns this chat's exclusive
        # lifetime lock. Its presence therefore proves every earlier generation
        # released that lock, even when its launcher died before writing a stop.
        changed.extend(
            _close_abandoned_generations(
                generations,
                open_generations,
                chat_id,
                generation,
                event.started_at,
            )
        )
    rows = generations.setdefault((chat_id, generation), {})
    if project_session_event(rows, event):
        if isinstance(event, SessionStartEvent):
            rows[chat_id] = rows[chat_id].model_copy(
                update={
                    field: getattr(records[chat_id], field)
                    for field in (
                        "created_at",
                        "forked_from_chat_id",
                        "forked_from_history_id",
                        "context_from_chat_id",
                        "context_from_history_id",
                    )
                }
            )
        changed.append((chat_id, generation))
        current = rows[chat_id]
        if current.record_mode == "live" and current.stopped_at is None:
            open_generations.setdefault(chat_id, set()).add(generation)
        else:
            remaining = open_generations.get(chat_id)
            if remaining is not None:
                remaining.discard(generation)
                if not remaining:
                    del open_generations[chat_id]
    return tuple(changed)


def session_instance_for_event(
    held_generation: str | None,
    lease_generation: str,
    record: SessionRecord | None,
) -> str:
    """Choose the held, leased, then journal generation without performing I/O."""
    if held_generation is not None:
        return held_generation
    if lease_generation.strip():
        return lease_generation
    return record.session_instance_id if record is not None else ""


def with_key[T: (SessionRecord, SessionStartEvent)](model: T, key: NativeKeyFields) -> T:
    """Carry accepted key fields into a new start, preserving legacy empty spelling."""
    return model.model_copy(
        update={
            "harness": key.harness or model.harness,
            "harness_session_id": key.session_id or model.harness_session_id,
            "native_store": key.native_store or model.native_store,
        }
    )


def by_native_key(
    records: Mapping[str, SessionRecord],
) -> dict[NativeKey, tuple[SessionRecord, ...]]:
    """Invert accepted chat records by complete key, retaining aliases in input order.

    Incomplete bindings are omitted. Values retain all chats sharing a native key;
    callers, not the fold, choose among aliases. No I/O or additional replay.
    """
    grouped: dict[NativeKey, list[SessionRecord]] = {}
    for record in records.values():
        key = record.native_key()
        if key is not None:
            grouped.setdefault(key, []).append(record)
    return {key: tuple(rows) for key, rows in grouped.items()}
