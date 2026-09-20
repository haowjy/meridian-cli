"""File-backed session tracking for a Meridian state root's `sessions.jsonl`."""

import json
import os
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import IO, Any, Literal, NamedTuple, Self, cast

import psutil
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from meridian.lib.core.types import (
    ChatId,
    HarnessSessionId,
    OptionalPersistedChatId,
    OptionalPersistedHarnessSessionId,
    PersistedChatId,
)
from meridian.lib.platform.locking import (
    acquire_file_lock,
    lock_file,
    release_file_lock,
    try_lock_file,
    unlink_validated_lock,
)
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import append_event, read_events, utc_now_iso
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.paths import RuntimePaths, normalize_path_for_write


def _append_session_event(
    data_path: Path,
    lock_path: Path,
    event: BaseModel,
    *,
    exclude_none: bool = False,
) -> None:
    changes = HistoryChanges(data_path.parent)
    with lock_file(changes.mutation_lock, mode="shared"), lock_file(lock_path):
        if isinstance(event, (SessionUpdateEvent, SessionStopEvent, SessionModelSelectionEvent)):
            for record in list_session_generations(data_path.parent):
                if (record.chat_id, record.session_instance_id) == (
                    event.chat_id,
                    event.session_instance_id,
                ) and record.record_mode == "historical":
                    raise ValueError("Historical sessions are inert and cannot be mutated")
        changes.mark(HistorySource(kind="sessions"))
        append_event(data_path, lock_path, event, exclude_none=exclude_none)


class _SessionLockHandles(NamedTuple):
    session: IO[bytes]
    project_lifetime: IO[bytes]
    session_instance_id: str


_SESSION_LOCK_HANDLES: dict[tuple[Path, str], _SessionLockHandles] = {}


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
    model_config = ConfigDict(extra="ignore")

    history_id: uuid.UUID | None = None
    v: int = 1
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
    claude_config_dir: str | None = None
    active_work_id: str | None = None
    spawn_id: str | None = None
    history_id: uuid.UUID | None = None
    startup_attempt_id: str | None = None


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
            return SessionModelSelectionEvent.model_validate(payload)
    except ValidationError:
        return None
    return None


def _parse_model_observation(payload: dict[str, Any]) -> SessionModelObservationEvent | None:
    if payload.get("event") != "model_observation":
        return None
    return SessionModelObservationEvent.model_validate(payload)


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


def _session_lease_path(paths: RuntimePaths, chat_id: str) -> Path:
    return paths.sessions_dir / f"{chat_id}.lease.json"


def _normalized_generation(generation: str) -> str:
    return generation.strip()


def _generation_matches(expected: str, actual: str) -> bool:
    normalized_expected = _normalized_generation(expected)
    normalized_actual = _normalized_generation(actual)
    if not normalized_expected and not normalized_actual:
        return True
    return normalized_expected == normalized_actual


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


def _read_session_counter(paths: RuntimePaths) -> int:
    if not paths.session_id_counter.is_file():
        return 0
    try:
        return int(paths.session_id_counter.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def reserve_chat_id(runtime_root: Path) -> str:
    paths = RuntimePaths.from_root_dir(runtime_root)
    with lock_file(paths.session_id_counter_flock):
        current = _read_session_counter(paths)
        next_value = current + 1
        atomic_write_text(paths.session_id_counter, f"{next_value}\n")
        return f"c{next_value}"


def project_session_event(records: dict[str, SessionRecord], event: SessionEvent) -> None:
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
        if event.harness_session_id not in session_ids:
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


def _records_by_session(runtime_root: Path) -> dict[str, SessionRecord]:
    paths = RuntimePaths.from_root_dir(runtime_root)
    records: dict[str, SessionRecord] = {}
    for event in read_events(paths.sessions_jsonl, _parse_event):
        project_session_event(records, event)
    return records


def _session_sort_key(chat_id: str) -> tuple[int, str]:
    if chat_id.startswith("c") and chat_id[1:].isdigit():
        return (int(chat_id[1:]), chat_id)
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

    paths = RuntimePaths.from_root_dir(runtime_root)
    project_lifetime_handle = acquire_file_lock(paths.project_lifetime_flock, mode="shared")
    resolved_chat_id = chat_id.strip() if chat_id is not None else ""
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
                ChatId(forked_from_chat_id) if forked_from_chat_id is not None else None
            ),
            spawn_id=spawn_id,
            forked_from_history_id=forked_from_history_id,
            model_selection_protocol=model_selection_protocol,
        )
        with lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"):
            # Chat-only callers select the current generation. Resolved references
            # carry their exact portable ancestor and must never be re-resolved.
            if forked_from_chat_id and forked_from_history_id is None:
                source = get_session_record(runtime_root, forked_from_chat_id)
                event = event.model_copy(
                    update={"forked_from_history_id": source.history_id if source else None}
                )
            if spawn_id is not None:
                from meridian.lib.state.spawn.model import SpawnRecord
                from meridian.lib.state.spawn.repository import Applied, write_state_locked

                def bind(current: SpawnRecord) -> SpawnRecord:
                    return current.model_copy(
                        update={
                            "chat_id": event.chat_id,
                            "session_instance_id": event.session_instance_id,
                            "forked_from_history_id": event.forked_from_history_id
                            or current.forked_from_history_id,
                        }
                    )

                binding = write_state_locked(
                    paths.spawns_dir, spawn_id, bind, allow_terminal_overwrite=True
                )
                if isinstance(binding, Applied):
                    event = event.model_copy(
                        update={
                            "history_id": binding.after.history_id,
                            "forked_from_history_id": binding.after.forked_from_history_id,
                        }
                    )
            _append_session_event(paths.sessions_jsonl, paths.sessions_flock, event)
            _write_session_lease(paths, resolved_chat_id, session_instance_id)
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

    paths = RuntimePaths.from_root_dir(runtime_root)
    session_instance_id = _session_instance_for_event(paths, runtime_root, chat_id)
    event = SessionStopEvent(
        chat_id=ChatId(chat_id),
        session_instance_id=session_instance_id,
        stopped_at=utc_now_iso(),
    )
    with (
        lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
        lock_file(paths.sessions_flock),
    ):
        _append_session_event(
            paths.sessions_jsonl,
            paths.sessions_flock,
            event,
            exclude_none=True,
        )
        _session_lease_path(paths, chat_id).unlink(missing_ok=True)
    _release_session_lock(runtime_root, chat_id)


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
    paths = RuntimePaths.from_root_dir(runtime_root)
    event = SessionUpdateEvent(
        chat_id=ChatId(chat_id),
        harness_session_id=HarnessSessionId(harness_session_id),
        session_instance_id=(
            session_instance_id
            if session_instance_id is not None
            else _session_instance_for_event(paths, runtime_root, chat_id)
        ),
        startup_attempt_id=startup_attempt_id,
    )
    if startup_attempt_id is not None:
        _validate_startup_identity(read_events(paths.sessions_jsonl, _parse_event), event)
    _append_session_event(
        paths.sessions_jsonl,
        paths.sessions_flock,
        event,
        exclude_none=True,
    )


def update_session_work_id(runtime_root: Path, chat_id: str, work_id: str | None) -> None:
    """Set or clear the active work item for a session."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    normalized_work_id = work_id.strip() if work_id is not None else ""
    event = SessionUpdateEvent(
        chat_id=ChatId(chat_id),
        harness_session_id=None,
        session_instance_id=_session_instance_for_event(paths, runtime_root, chat_id),
        active_work_id=normalized_work_id,
    )
    _append_session_event(
        paths.sessions_jsonl,
        paths.sessions_flock,
        event,
        exclude_none=True,
    )


def update_session_spawn_id(runtime_root: Path, chat_id: str, spawn_id: str) -> None:
    """Record the canonical primary spawn relationship for a session."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    from meridian.lib.state.spawn.repository import read_state

    spawn = read_state(paths.spawns_dir, spawn_id.strip(), include_prompt=False)
    event = SessionUpdateEvent(
        chat_id=ChatId(chat_id),
        harness_session_id=None,
        session_instance_id=_session_instance_for_event(paths, runtime_root, chat_id),
        spawn_id=spawn_id.strip(),
        history_id=spawn.history_id if spawn is not None else None,
    )
    _append_session_event(
        paths.sessions_jsonl,
        paths.sessions_flock,
        event,
        exclude_none=True,
    )


def update_session_claude_config_dir(
    runtime_root: Path,
    chat_id: str,
    claude_config_dir: str,
) -> None:
    """Append a session update event carrying the isolated Claude config dir."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    event = SessionUpdateEvent(
        chat_id=ChatId(chat_id),
        harness_session_id=None,
        session_instance_id=_session_instance_for_event(paths, runtime_root, chat_id),
        claude_config_dir=claude_config_dir,
    )
    _append_session_event(
        paths.sessions_jsonl,
        paths.sessions_flock,
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

    return _records_by_session(runtime_root).get(chat_id)


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


def _validate_startup_identity(
    events: list[SessionEvent], event: SessionUpdateEvent | SessionModelSelectionEvent,
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


def get_initial_model_selection(
    runtime_root: Path, harness: str, harness_session_id: str,
    *, source_chat_id: str | None = None,
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
        event for event in events
        if isinstance(event, SessionStartEvent) and event.harness == harness
    ]
    start = next((
        event for event in starts
        if event.harness_session_id == harness_session_id or any(
            update.harness_session_id == harness_session_id
            for update in updates.get((event.chat_id, event.session_instance_id), [])
        )
    ), None)
    origin_chat_id = start.chat_id if start is not None else source_chat_id
    if origin_chat_id is not None:
        start = next((event for event in starts if event.chat_id == origin_chat_id), None)
    if start is None or start.model_selection_protocol is not None:
        return None
    generation_updates = updates.get((start.chat_id, start.session_instance_id), [])
    spawn_id = start.spawn_id or next(
        (update.spawn_id for update in generation_updates if update.spawn_id), None,
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
        selection = ConversationModelSelection(
            requested_token=snapshot.model_selection_requested_token or snapshot.model or None,
            selected_token=snapshot.model_selection_selected_token or snapshot.model or None,
            canonical_model_id=canonical,
            harness_model_id=executable,
            model_mode=(
                "named" if canonical and executable else
                "harness_default" if not snapshot.model else None
            ),
            provider_constraint=snapshot.model_selection_provider_constraint,
            selection_source="initial_launch",
            provenance=snapshot.field_provenance,
        )
    return SessionModelSelectionEvent(
        kind="initial_seed", harness=harness,
        harness_session_id=HarnessSessionId(harness_session_id),
        chat_id=start.chat_id, session_instance_id=start.session_instance_id,
        spawn_id=spawn_id, startup_attempt_id=None,
        recorded_at=utc_now_iso(), selection=selection,
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


def record_model_selection(runtime_root: Path, event: SessionModelSelectionEvent) -> bool:
    """Durably append once per invocation/conversation; false means already recorded."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    with lock_file(paths.project_lifetime_flock, mode="shared"):
        if not runtime_root.is_dir():
            raise FileNotFoundError(runtime_root)
        with lock_file(paths.sessions_flock):
            events = read_events(paths.sessions_jsonl, _parse_event)
            source_start = next((
                start for start in events
                if isinstance(start, SessionStartEvent)
                and start.chat_id == event.chat_id
                and start.session_instance_id == event.session_instance_id
                and start.harness == event.harness
            ), None)
            if source_start is None:
                raise ValueError("selection has no matching captured session generation")
            if event.kind == "initial_seed" and source_start.model_selection_protocol is not None:
                raise ValueError("cannot seed a new-protocol session from prelaunch intent")
            _validate_startup_identity(events, event)
            bound = _bound_model_selections([*events, event])
            _, native_id = bound[-1]
            for prior, prior_id in bound[:-1]:
                if prior.harness != event.harness:
                    continue
                if native_id is not None and prior_id == native_id and (
                    event.kind == "initial_seed" or (
                        prior.kind == "invocation_started" and prior.spawn_id == event.spawn_id
                    )
                ):
                    if event.kind == "invocation_started" and not any(
                        isinstance(identity, (SessionUpdateEvent, SessionModelSelectionEvent))
                        and identity.chat_id == event.chat_id
                        and identity.session_instance_id == event.session_instance_id
                        and identity.startup_attempt_id == event.startup_attempt_id
                        and identity.harness_session_id == native_id
                        for identity in events
                    ):
                        append_event(paths.sessions_jsonl, paths.sessions_flock, SessionUpdateEvent(
                            chat_id=event.chat_id,
                            session_instance_id=event.session_instance_id,
                            startup_attempt_id=event.startup_attempt_id,
                            harness_session_id=HarnessSessionId(native_id),
                        ), exclude_none=True)
                    return False
                if event.kind == "invocation_started" and (
                    prior.kind == event.kind
                    and prior.spawn_id == event.spawn_id
                    and prior.chat_id == event.chat_id
                    and prior.session_instance_id == event.session_instance_id
                    and prior.startup_attempt_id == event.startup_attempt_id
                ):
                    if prior_id is not None and native_id is not None and prior_id != native_id:
                        raise ValueError("startup attempt changed its native conversation identity")
                    return False
            append_event(paths.sessions_jsonl, paths.sessions_flock, event)
            return True


def record_model_observation(runtime_root: Path, event: SessionModelObservationEvent) -> bool:
    """Durably append an executed-model observation; false means already recorded.

    Independent of intent: this fact keys on ``(harness, harness_session_id)`` and
    may exist for native sessions Meridian did not start, so no matching
    ``SessionStartEvent`` generation is required. Dedupes against the latest
    observation for the identity to keep the JSONL bounded.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    with lock_file(paths.project_lifetime_flock, mode="shared"):
        if not runtime_root.is_dir():
            raise FileNotFoundError(runtime_root)
        with lock_file(paths.sessions_flock):
            if (
                get_last_executed_model(runtime_root, event.harness, event.harness_session_id)
                == event.observed_model_token
            ):
                return False
            append_event(paths.sessions_jsonl, paths.sessions_flock, event)
            return True


def get_last_executed_model(
    runtime_root: Path, harness: str, harness_session_id: str,
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
    runtime_root: Path, ref: str, *, harness: str | None = None,
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
        with (
            lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
            lock_file(paths.sessions_flock),
        ):
            records = _records_by_session(runtime_root)
            stopped_at = utc_now_iso()
            for chat_id, _lock_path, _ in stale:
                existing = records.get(chat_id)
                (
                    lease_exists,
                    lease_session_instance_id,
                    lease_owner_pid,
                    lease_owner_birth,
                ) = _read_session_lease_data(paths, chat_id)
                if (
                    existing is not None
                    and existing.kind == "primary"
                    and lease_exists
                    and lease_owner_pid is not None
                    and is_process_alive_with_birth(lease_owner_pid, lease_owner_birth)
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
                    _append_session_event(
                        paths.sessions_jsonl,
                        paths.sessions_flock,
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
                _session_lease_path(paths, chat_id).unlink(missing_ok=True)

        cleaned_id_set = frozenset(cleaned_ids)
        for chat_id, lock_path, handle in stale:
            if chat_id in cleaned_id_set:
                unlink_validated_lock(lock_path, handle)

    return StaleSessionCleanup(
        cleaned_ids=tuple(sorted(cleaned_ids, key=_session_sort_key)),
        materialized_scopes=tuple(sorted(set(stale_cleanup_scopes))),
    )


def append_historical_session(runtime_root: Path, record: SessionRecord) -> None:
    paths = RuntimePaths.from_root_dir(runtime_root)
    with (
        lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
        lock_file(paths.sessions_flock),
    ):
        existing = _records_by_session(runtime_root).get(record.chat_id)
        if existing is not None:
            if existing == record:
                return
            raise ValueError("Historical session alias conflict")
        _append_session_event(
            paths.sessions_jsonl, paths.sessions_flock, SessionHistoricalEvent(record=record)
        )


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
