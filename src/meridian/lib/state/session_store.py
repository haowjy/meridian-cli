"""File-backed session tracking for a Meridian state root's `sessions.jsonl`."""

import json
import os
import re
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import IO, Annotated, Literal, NamedTuple, cast

import psutil
from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.native_identity import (
    BindSource,
    NativeEntryMismatch,
    NativeKeyFields,
)
from meridian.lib.core.types import (
    ChatId,
    HarnessSessionId,
)
from meridian.lib.platform.locking import (
    acquire_file_lock,
    lock_file,
    release_file_lock,
    try_lock_file,
    unlink_validated_lock,
)
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import read_events, utc_now_iso
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.json_doc import read_model, write_model
from meridian.lib.state.jsonl import append_durable_jsonl_line
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.native_binding import BindOutcome, Conflict, bind, report_conflict
from meridian.lib.state.paths import RuntimePaths, normalize_path_for_write
from meridian.lib.state.session_fold import (
    ConversationModelSelection as ConversationModelSelection,
)
from meridian.lib.state.session_fold import (
    SessionEvent as SessionEvent,
)
from meridian.lib.state.session_fold import (
    SessionHistoricalEvent as SessionHistoricalEvent,
)
from meridian.lib.state.session_fold import (
    SessionModelObservationEvent as SessionModelObservationEvent,
)
from meridian.lib.state.session_fold import (
    SessionModelSelectionEvent as SessionModelSelectionEvent,
)
from meridian.lib.state.session_fold import (
    SessionRecord as SessionRecord,
)
from meridian.lib.state.session_fold import (
    SessionSettings,
    fold_session_generations,
    generation_matches,
    is_historical_generation,
    parse_event,
    session_instance_for_event,
    validate_historical_import,
    validate_startup_identity,
    with_key,
)
from meridian.lib.state.session_fold import (
    SessionStartEvent as SessionStartEvent,
)
from meridian.lib.state.session_fold import (
    SessionStopEvent as SessionStopEvent,
)
from meridian.lib.state.session_fold import (
    SessionUpdateEvent as SessionUpdateEvent,
)
from meridian.lib.state.session_fold import (
    project_session_event as project_session_event,
)
from meridian.lib.utils.json_shapes import (
    float_or_none,
    int_or_none,
    json_list,
    json_object,
    string_or_empty,
)

# A ``"chat_id"`` key, any JSON whitespace, then one complete JSON string token
# (escapes included). Matches end at that token's closing quote, so no match can
# swallow a later key.
_SESSION_CHAT_ID_FIELD = re.compile(rb'"chat_id"\s*:\s*"((?:[^"\\]|\\.)*)"')
# Only ``\u00XX`` escapes can spell a letter of ``chat_id`` (c h a t _ i d).
_ESCAPED_CHAT_ID_KEY_LETTER = re.compile(rb"\\u00(?:6[13489]|74|5[fF])")


def _row_may_select_chat(raw: bytes, chat_ids: Collection[str]) -> bool:
    """Byte prefilter for selected replay. It may only nominate, never decide.

    A false positive costs one ``json.loads``; a false negative silently drops
    history, so this must return True for every row the decoded check in
    ``read_session_events`` would select. It applies the same predicate
    (decoded value, ``strip()``, membership) to every ``chat_id`` string token
    in the row, which covers a ``historical_import`` row's nested ``record``,
    whitespace padding, escaped or raw non-ASCII values, key spacing, and
    duplicate keys (the decoder keeps the last). Rows the regex cannot see
    into are nominated unread: a NUL byte means ``json.loads`` may detect
    UTF-16/32, and an escaped key letter could spell ``chat_id``.
    """
    if b"\x00" in raw or _ESCAPED_CHAT_ID_KEY_LETTER.search(raw) is not None:
        return True
    for match in _SESSION_CHAT_ID_FIELD.finditer(raw):
        token = match.group(1)
        try:
            value: str = json.loads(b'"' + token + b'"') if b"\\" in token else token.decode()
        except ValueError:  # Includes UnicodeDecodeError; let the decoder judge it.
            return True
        if value.strip() in chat_ids:
            return True
    return False


def native_ids(row: Mapping[str, object]) -> frozenset[str]:
    """All native keys named by a raw row, including pre-schema legacy arrays."""
    record = json_object(row.get("record")) if row.get("event") == "historical_import" else row
    if record is None:
        return frozenset()
    values = [record.get("harness_session_id")]
    legacy = json_list(record.get("harness_session_ids"))
    if legacy is not None:
        values.extend(legacy)
    return frozenset(value.strip() for value in values if isinstance(value, str) and value.strip())


def _row_may_name_native(raw: bytes, native_id: str) -> bool:
    # Escapes and non-UTF8 JSON may spell a key/value differently. Nominate them;
    # only native_ids on the decoded object decides membership.
    return b"\\" in raw or b"\x00" in raw or native_id.encode() in raw


def chat_rows(root: Path, chat: str) -> list[SessionEvent]:
    return read_session_events(root, {chat})


def chat_record(root: Path, chat: str) -> SessionRecord | None:
    records: dict[str, SessionRecord] = {}
    for event in chat_rows(root, chat):
        project_session_event(records, event)
    return records.get(chat)


def chat_generations(root: Path, chat: str) -> tuple[SessionRecord, ...]:
    return fold_session_generations(chat_rows(root, chat))


def native_chats(root: Path, native_id: str) -> tuple[str, ...]:
    """Nominate chats naming a native key; callers verify their own exact facts."""
    def select(row: dict[str, object]) -> str | None:
        if native_id not in native_ids(row):
            return None
        record = json_object(row.get("record")) if row.get("event") == "historical_import" else row
        if record is None:
            return None
        chat = record.get("chat_id")
        return chat.strip() if isinstance(chat, str) and chat.strip() else None

    return tuple(sorted(set(read_events(
        RuntimePaths.from_root_dir(root).sessions_jsonl, select,
        raw_filter=lambda raw: _row_may_name_native(raw, native_id),
    )), key=_session_sort_key))


def native_observations(root: Path, native_id: str) -> list[SessionModelObservationEvent]:
    def select(row: dict[str, object]) -> SessionModelObservationEvent | None:
        if native_id not in native_ids(row):
            return None
        return _parse_model_observation(row)

    return read_events(
        RuntimePaths.from_root_dir(root).sessions_jsonl, select,
        raw_filter=lambda raw: _row_may_name_native(raw, native_id),
    )


def append_chat_rows(root: Path, chat: str, rows: Iterable[SessionEvent]) -> None:
    """Consume rows under mutation → sessions locks; generators may validate/dedup here."""
    paths = RuntimePaths.from_root_dir(root)
    with (
        lock_file(HistoryChanges(root).mutation_lock, mode="shared"),
        lock_file(paths.sessions_flock),
    ):
        for row in rows:
            if row.chat_id != chat:
                raise ValueError("Session append names a different chat")
            _append_session_event(
                paths.sessions_jsonl, row,
                exclude_none=isinstance(row, (SessionUpdateEvent, SessionStopEvent)),
            )


def append_native_rows(
    root: Path, native_id: str, rows: Iterable[SessionModelObservationEvent],
) -> None:
    paths = RuntimePaths.from_root_dir(root)
    with (
        lock_file(HistoryChanges(root).mutation_lock, mode="shared"),
        lock_file(paths.sessions_flock),
    ):
        for row in rows:
            if row.harness_session_id != native_id:
                raise ValueError("Observation append names a different native session")
            _append_session_event(
                paths.sessions_jsonl, row, exclude_none=True,
            )


def _native_scope(
    root: Path, native_id: str | None, *, source_chat_id: str | None = None,
) -> list[SessionEvent]:
    chats: set[str] = set(native_chats(root, native_id)) if native_id else set()
    if source_chat_id is not None:
        chats.add(source_chat_id)
    rows = [
        (event, index) for chat in chats for index, event in enumerate(chat_rows(root, chat))
    ]
    # Only starts and selections have cross-chat order semantics; updates are
    # keyed by generation and startup attempt, independently of their position.
    rows.sort(key=lambda item: (
        item[0].started_at if isinstance(item[0], SessionStartEvent) else
        item[0].recorded_at if isinstance(item[0], SessionModelSelectionEvent) else "",
        _session_sort_key(item[0].chat_id), item[1],
    ))
    return [event for event, _ in rows]


class StartedSession(NamedTuple):
    chat_id: str
    generation: str
    record: SessionRecord


def _append_session_event(
    data_path: Path,
    event: BaseModel,
    *,
    exclude_none: bool = False,
) -> None:
    changes = HistoryChanges(data_path.parent)
    if isinstance(event, SessionHistoricalEvent):
        validate_historical_import(event)
        if not is_historical_generation(event.session_instance_id):
            raise ValueError("Historical imports must use the reserved generation prefix")
    if isinstance(event, SessionStartEvent) and is_historical_generation(
        event.session_instance_id
    ):
        raise ValueError("Live sessions cannot use the reserved historical generation prefix")
    if isinstance(
        event, (SessionUpdateEvent, SessionStopEvent, SessionModelSelectionEvent)
    ) and is_historical_generation(event.session_instance_id):
        raise ValueError("Historical sessions are inert and cannot be mutated")
    changes.mark(HistorySource(kind="sessions"))
    # Inherited policy can include configured environment values and prompts.
    if not data_path.exists():
        atomic_write_text(data_path, "")
    data_path.chmod(0o600)
    payload = event.model_dump(mode="json", exclude_none=exclude_none)
    append_durable_jsonl_line(data_path, json.dumps(payload, separators=(",", ":"), sort_keys=True))


class _SessionLockHandles(NamedTuple):
    session: IO[bytes]
    project_lifetime: IO[bytes]
    session_instance_id: str


_SESSION_LOCK_HANDLES: dict[tuple[Path, str], _SessionLockHandles] = {}


type MaterializedCleanupScope = str


class StaleSessionCleanup(NamedTuple):
    cleaned_ids: tuple[str, ...]
    materialized_scopes: tuple[MaterializedCleanupScope, ...]


def _parse_model_observation(payload: Mapping[str, object]) -> SessionModelObservationEvent | None:
    if payload.get("event") != "model_observation":
        return None
    try:
        return SessionModelObservationEvent.model_validate(payload)
    except ValidationError:
        return None


def _session_lease_path(paths: RuntimePaths, chat_id: str) -> Path:
    return paths.sessions_dir / f"{chat_id}.lease.json"


class SessionLease(BaseModel):
    """Best-effort ownership metadata for one active chat lock."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    chat_id: Annotated[str, BeforeValidator(string_or_empty)] = ""
    owner_created_at_epoch: Annotated[float | None, BeforeValidator(float_or_none)] = None
    owner_pid: Annotated[int | None, BeforeValidator(int_or_none)] = None
    session_instance_id: Annotated[str, BeforeValidator(string_or_empty)] = ""


def _read_session_lease_data(
    paths: RuntimePaths,
    chat_id: str,
) -> SessionLease | None:
    return read_model(_session_lease_path(paths, chat_id), SessionLease)


def _read_session_lease(paths: RuntimePaths, chat_id: str) -> tuple[bool, str]:
    lease = _read_session_lease_data(paths, chat_id)
    return (lease is not None, lease.session_instance_id if lease is not None else "")


def _write_session_lease(paths: RuntimePaths, chat_id: str, session_instance_id: str) -> None:
    try:
        owner_created_at_epoch: float | None = psutil.Process(os.getpid()).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        owner_created_at_epoch = None
    write_model(
        _session_lease_path(paths, chat_id),
        SessionLease(
            chat_id=chat_id,
            owner_pid=os.getpid(),
            owner_created_at_epoch=owner_created_at_epoch,
            session_instance_id=session_instance_id,
        ),
    )


def resolve_session_instance_id(paths: RuntimePaths, runtime_root: Path, chat_id: str) -> str:
    held = _SESSION_LOCK_HANDLES.get(_session_lock_key(runtime_root, chat_id))
    held_generation = held.session_instance_id if held is not None else None
    lease_generation = ""
    record = None
    if held_generation is None:
        _, lease_generation = _read_session_lease(paths, chat_id)
        if not lease_generation.strip():
            record = get_session_record(runtime_root, chat_id)
    return session_instance_for_event(held_generation, lease_generation, record)


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


def read_session_events(
    runtime_root: Path, chat_ids: Collection[str] | None = None
) -> list[SessionEvent]:
    """Parse every session event in source order, or only those of ``chat_ids``.

    One chat's projection reads no other chat's events, so folding the selected
    events equals folding all of them for those chats. The one intended
    difference: a full fold raises on any chat whose historical record is
    followed by a mutation, while a selected fold raises only when that chat is
    selected, so a poisoned chat no longer blocks other chats' reads and binds.
    """
    paths = RuntimePaths.from_root_dir(runtime_root)

    def selected_event(payload: Mapping[str, object]) -> SessionEvent | None:
        # This check decides selection; _row_may_select_chat must nominate
        # every row it accepts, so change both together.
        if chat_ids is not None:
            record = (
                payload.get("record") if payload.get("event") == "historical_import" else payload
            )
            raw_id = (
                cast("dict[str, object]", record).get("chat_id")
                if isinstance(record, dict) else None
            )
            if not isinstance(raw_id, str) or raw_id.strip() not in chat_ids:
                return None
        return parse_event(payload)

    return read_events(
        paths.sessions_jsonl,
        selected_event,
        raw_filter=None if chat_ids is None else lambda raw: _row_may_select_chat(raw, chat_ids),
    )


def _records_by_session(
    runtime_root: Path, *, chat_ids: Collection[str] | None = None
) -> dict[str, SessionRecord]:
    records: dict[str, SessionRecord] = {}
    for event in read_session_events(runtime_root, chat_ids):
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
    if expected is None or not generation_matches(
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
    native_store: str | None = None,
    claude_config_dir: str | None = None,
    kind: Literal["primary", "spawn"] = "spawn",
    spawn_id: str | None = None,
    model_selection_protocol: Literal[1] | None = None,
) -> StartedSession:
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
            native_store=native_store,
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
        with (
            lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
            lock_file(paths.sessions_flock),
        ):
            existing = get_session_record(runtime_root, resolved_chat_id)
            if existing is not None:
                from meridian.lib.state.session_identity import live_observer_for_chat

                observer = live_observer_for_chat(
                    runtime_root, resolved_chat_id, excluding_spawn_id=spawn_id,
                )
                if observer is not None:
                    raise ValueError(f"Chat {resolved_chat_id} is in use by live run {observer}")
                outcome = bind(existing.key_fields(), event.key_fields())
                if isinstance(outcome, Conflict):
                    report_conflict(event.chat_id, outcome, "start")
                    raise NativeEntryMismatch(outcome.kept, outcome.attempted)
                event = with_key(event, outcome.key)
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

                def bind_spawn(current: SpawnRecord) -> SpawnRecord:
                    return current.model_copy(
                        update={
                            "chat_id": event.chat_id,
                            "session_instance_id": event.session_instance_id,
                            "forked_from_history_id": event.forked_from_history_id
                            or current.forked_from_history_id,
                        }
                    )

                binding = write_state_locked(
                    paths.spawns_dir, spawn_id, bind_spawn, allow_terminal_overwrite=True
                )
                if isinstance(binding, Applied):
                    event = event.model_copy(
                        update={
                            "history_id": binding.after.history_id,
                            "forked_from_history_id": binding.after.forked_from_history_id,
                        }
                    )
            append_chat_rows(runtime_root, resolved_chat_id, [event])
            projected = {resolved_chat_id: existing} if existing is not None else {}
            project_session_event(projected, event)
            record = projected[resolved_chat_id]
            _write_session_lease(paths, resolved_chat_id, session_instance_id)
    except Exception:
        if handle is not None:
            release_file_lock(handle)
        release_file_lock(project_lifetime_handle)
        raise

    assert handle is not None
    _SESSION_LOCK_HANDLES[_session_lock_key(runtime_root, resolved_chat_id)] = _SessionLockHandles(
        session=handle,
        project_lifetime=project_lifetime_handle,
        session_instance_id=session_instance_id,
    )
    return StartedSession(resolved_chat_id, session_instance_id, record)


def stop_session(runtime_root: Path, chat_id: str) -> None:
    """Append a session stop event and release the lifetime session lock."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    session_instance_id = resolve_session_instance_id(paths, runtime_root, chat_id)
    event = SessionStopEvent(
        chat_id=ChatId(chat_id),
        session_instance_id=session_instance_id,
        stopped_at=utc_now_iso(),
    )
    with (
        lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
        lock_file(paths.sessions_flock),
    ):
        append_chat_rows(runtime_root, chat_id, [event])
        _session_lease_path(paths, chat_id).unlink(missing_ok=True)
    _release_session_lock(runtime_root, chat_id)


def update_session_harness_id(
    runtime_root: Path,
    chat_id: str,
    attempted: NativeKeyFields,
    *,
    source: BindSource,
    session_instance_id: str | None = None,
    startup_attempt_id: str | None = None,
) -> BindOutcome:
    """Atomically bind the first native key; conflicts never append a rebind."""
    from meridian.lib.state.session_binding import bind_native_key

    return bind_native_key(
        runtime_root, chat_id, attempted, source=source,
        session_instance_id=session_instance_id, startup_attempt_id=startup_attempt_id,
    )


def update_session_work_id(runtime_root: Path, chat_id: str, work_id: str | None) -> None:
    """Set or clear the active work item for a session."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    normalized_work_id = work_id.strip() if work_id is not None else ""
    event = SessionUpdateEvent(
        chat_id=ChatId(chat_id),
        harness_session_id=None,
        session_instance_id=resolve_session_instance_id(paths, runtime_root, chat_id),
        active_work_id=normalized_work_id,
    )
    append_chat_rows(runtime_root, chat_id, [event])


def update_session_spawn_id(runtime_root: Path, chat_id: str, spawn_id: str) -> None:
    """Record the canonical primary spawn relationship for a session."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    from meridian.lib.state.spawn.repository import read_state

    spawn = read_state(paths.spawns_dir, spawn_id.strip(), include_prompt=False)
    event = SessionUpdateEvent(
        chat_id=ChatId(chat_id),
        harness_session_id=None,
        session_instance_id=resolve_session_instance_id(paths, runtime_root, chat_id),
        spawn_id=spawn_id.strip(),
        history_id=spawn.history_id if spawn is not None else None,
    )
    append_chat_rows(runtime_root, chat_id, [event])


def update_session_launch_policy(
    runtime_root: Path, chat_id: str, session_instance_id: str, snapshot: LaunchPolicySnapshot,
) -> None:
    """Make the final launch policy owned by this exact conversation generation."""

    event = SessionUpdateEvent(
        chat_id=ChatId(chat_id),
        harness_session_id=None,
        session_instance_id=session_instance_id,
        launch_policy_snapshot=snapshot,
    )
    append_chat_rows(runtime_root, chat_id, [event])


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
        session_instance_id=resolve_session_instance_id(paths, runtime_root, chat_id),
        claude_config_dir=claude_config_dir,
    )
    append_chat_rows(runtime_root, chat_id, [event])


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
        lease = _read_session_lease_data(paths, chat_id)
        if (
            lease is not None
            and lease.owner_pid is not None
            and is_process_alive_with_birth(lease.owner_pid, lease.owner_created_at_epoch)
        ):
            return True
    return False


def is_session_lease_owner_alive(
    runtime_root: Path, chat_id: str, *, session_instance_id: str | None = None
) -> bool:
    """Check a live lease, optionally requiring the exact session generation."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    lease = _read_session_lease_data(paths, chat_id)
    if lease is None:
        return False
    if session_instance_id is not None and lease.session_instance_id != session_instance_id:
        return False
    return lease.owner_pid is not None and is_process_alive_with_birth(
        lease.owner_pid,
        lease.owner_created_at_epoch,
    )


def list_active_session_records(runtime_root: Path) -> list[SessionRecord]:
    """Return materialized records for active sessions."""

    return [
        record for chat_id in list_active_sessions(runtime_root)
        if (record := chat_record(runtime_root, chat_id)) is not None
    ]


def list_all_session_records(runtime_root: Path) -> list[SessionRecord]:
    """Return all materialized records, including stopped sessions."""

    return list(_records_by_session(runtime_root).values())


def get_session_record(runtime_root: Path, chat_id: str | None) -> SessionRecord | None:
    """Return a materialized record for one chat ID, if present."""

    if chat_id is None:
        return None
    return chat_record(runtime_root, chat_id)


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
    runtime_root: Path, harness: str, harness_session_id: str,
    *, source_chat_id: str | None = None,
) -> SessionModelSelectionEvent | None:
    """Read a legacy conversation's original value without seeding or replaying attempts."""
    events = _native_scope(runtime_root, harness_session_id, source_chat_id=source_chat_id)
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
    snapshot = start.launch_policy_snapshot or _linked_spawn_policy(runtime_root, spawn_id)
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
                "named" if named else
                "harness_default" if not snapshot.model and not canonical else None
            ),
            provider_constraint=(
                snapshot.model_selection_provider_constraint if named else None
            ),
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

    current: ConversationModelSelection | None = None
    seed: ConversationModelSelection | None = None
    seen_invocations: set[str] = set()
    for event, native_id in _bound_model_selections(
        _native_scope(runtime_root, harness_session_id)
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

    recorded = False

    def rows() -> Iterator[SessionEvent]:
        nonlocal recorded
        source_rows = chat_rows(runtime_root, event.chat_id)
        _, native_id = _bound_model_selections([*source_rows, event])[-1]
        events = _native_scope(runtime_root, native_id, source_chat_id=event.chat_id)
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
        validate_startup_identity(events, event)
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
                    yield SessionUpdateEvent(
                        chat_id=event.chat_id,
                        session_instance_id=event.session_instance_id,
                        startup_attempt_id=event.startup_attempt_id,
                        harness_session_id=HarnessSessionId(native_id),
                    )
                return
            if event.kind == "invocation_started" and (
                prior.kind == event.kind
                and prior.spawn_id == event.spawn_id
                and prior.chat_id == event.chat_id
                and prior.session_instance_id == event.session_instance_id
                and prior.startup_attempt_id == event.startup_attempt_id
            ):
                if prior_id is not None and native_id is not None and prior_id != native_id:
                    raise ValueError("startup attempt changed its native conversation identity")
                return
        yield event
        recorded = True

    paths = RuntimePaths.from_root_dir(runtime_root)
    with lock_file(paths.project_lifetime_flock, mode="shared"):
        if not runtime_root.is_dir():
            raise FileNotFoundError(runtime_root)
        append_chat_rows(runtime_root, event.chat_id, rows())
    return recorded


def record_model_observation(runtime_root: Path, event: SessionModelObservationEvent) -> bool:
    """Durably append an executed-model observation; false means already recorded.

    Independent of intent: this fact keys on ``(harness, harness_session_id)`` and
    may exist for native sessions Meridian did not start, so no matching
    ``SessionStartEvent`` generation is required. Dedupes against the latest
    observation for the identity to keep the JSONL bounded.
    """

    recorded = False

    def rows() -> Iterator[SessionModelObservationEvent]:
        nonlocal recorded
        if (
            get_last_executed_model(runtime_root, event.harness, event.harness_session_id)
            != event.observed_model_token
        ):
            yield event
            recorded = True

    paths = RuntimePaths.from_root_dir(runtime_root)
    with lock_file(paths.project_lifetime_flock, mode="shared"):
        if not runtime_root.is_dir():
            raise FileNotFoundError(runtime_root)
        append_native_rows(runtime_root, event.harness_session_id, rows())
    return recorded


def get_last_executed_model(
    runtime_root: Path,
    harness: str,
    harness_session_id: str,
) -> str | None:
    """Return the latest observed executed model token for a native session, or None."""

    latest: str | None = None
    for event in native_observations(runtime_root, harness_session_id):
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
    selected = {chat_id.strip() for chat_id in chat_ids if chat_id.strip()}
    records = {
        chat: record for chat in selected
        if (record := chat_record(runtime_root, chat)) is not None
    }
    return [
        records[chat_id]
        for chat_id in sorted(
            selected,
            key=_session_sort_key,
        )
        if chat_id in records
    ]


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
        for chat in native_chats(runtime_root, normalized)
        for record in chat_generations(runtime_root, chat)
        if normalized == record.harness_session_id
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

    record = get_session_record(runtime_root, chat_id)
    if record is None:
        return None
    return record.active_work_id


def get_session_harness_id(runtime_root: Path, chat_id: str) -> str | None:
    """Return harness session ID for a Meridian session ID."""

    record = get_session_record(runtime_root, chat_id)
    if record is None:
        return None
    return record.harness_session_id


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
            records = {
                chat: record for chat, _, _ in stale
                if (record := chat_record(runtime_root, chat)) is not None
            }
            stopped_at = utc_now_iso()
            for chat_id, _lock_path, _ in stale:
                existing = records.get(chat_id)
                lease = _read_session_lease_data(paths, chat_id)
                if (
                    existing is not None
                    and existing.kind == "primary"
                    and lease is not None
                    and lease.owner_pid is not None
                    and is_process_alive_with_birth(
                        lease.owner_pid,
                        lease.owner_created_at_epoch,
                    )
                ):
                    continue
                stop_session_instance_id = lease.session_instance_id if lease is not None else ""
                if lease is None and existing is not None:
                    stop_session_instance_id = existing.session_instance_id
                if (
                    existing is not None
                    and existing.stopped_at is None
                    and (
                        lease is None
                        or generation_matches(
                            existing.session_instance_id,
                            lease.session_instance_id,
                        )
                    )
                ):
                    append_chat_rows(runtime_root, chat_id, [SessionStopEvent(
                            chat_id=ChatId(chat_id),
                            session_instance_id=stop_session_instance_id,
                            stopped_at=stopped_at,
                        )])
                    records[chat_id] = existing.model_copy(update={"stopped_at": stopped_at})

                should_clean = (
                    existing is None or existing.stopped_at is not None or lease is None
                )
                if existing is not None and existing.stopped_at is None:
                    should_clean = lease is None or generation_matches(
                        existing.session_instance_id,
                        lease.session_instance_id,
                    )
                if not should_clean:
                    continue

                if existing is not None and existing.harness.strip():
                    stale_cleanup_scopes.append(existing.harness.strip())
                cleaned_ids.append(chat_id)
                cleaned_generation = (
                    lease.session_instance_id
                    if lease is not None
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
        existing = get_session_record(runtime_root, record.chat_id)
        if existing is not None:
            if existing == record:
                return
            raise ValueError("Historical session alias conflict")
        append_chat_rows(runtime_root, record.chat_id, [SessionHistoricalEvent(record=record)])


def get_session_launch_policy_snapshot(
    runtime_root: Path, record: SessionRecord,
) -> LaunchPolicySnapshot | None:
    """The generation's own policy; pre-snapshot generations read their exact run."""
    return record.launch_policy_snapshot or _linked_spawn_policy(runtime_root, record.spawn_id)


def _linked_spawn_policy(runtime_root: Path, spawn_id: str | None) -> LaunchPolicySnapshot | None:
    """Bounded legacy read: an exact recorded link, never a newest-owner guess."""
    from meridian.lib.state import spawn_store

    row = spawn_store.get_spawn(runtime_root, spawn_id) if spawn_id else None
    return row.launch_policy_snapshot if row is not None else None


def get_or_create_observed_chat(
    runtime_root: Path,
    source_chat_id: str,
    harness: str,
    native_store: str,
    session_id: str,
    *,
    spawn_id: str,
    native_exists: Callable[[], bool] | None = None,
) -> str | None:
    """Materialize and publish the current chat in one source-lock epoch.

    The single continuation writer for owned live observations and verified
    post-exit keys. The caller admits native evidence; this seam owns context
    inheritance and publication. ``native_exists`` is the post-exit proof: it
    runs under the locks before any alias is reused or created, and a refusal
    publishes nothing (``None``). A missing/deleted run cannot create an orphan
    chat. Settings come from the current pointer read under the lock, not a
    stale caller read. Session events and spawn state remain separate files.
    """
    from meridian.lib.state.spawn.model import SpawnRecord
    from meridian.lib.state.spawn.repository import (
        Applied,
        Decline,
        Declined,
        write_state_locked,
    )

    paths = RuntimePaths.from_root_dir(runtime_root)
    with (
        lock_file(paths.project_lifetime_flock, mode="shared"),
        lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
        lock_file(paths.sessions_flock),
    ):
        def observe(row: SpawnRecord) -> SpawnRecord | Decline:
            if native_exists is not None and not native_exists():
                return Decline("native session unproven")
            chat_id = _get_or_create_observed_chat_locked(
                runtime_root, row.current_chat_id or source_chat_id,
                harness=harness, native_store=native_store, session_id=session_id,
            )
            return row.model_copy(update={"current_chat_id": chat_id})

        result = write_state_locked(
            paths.spawns_dir, spawn_id, observe, allow_terminal_overwrite=True,
        )
        if isinstance(result, Applied):
            assert result.after.current_chat_id is not None
            return result.after.current_chat_id
        if isinstance(result, Declined):
            return None
        raise ValueError(f"Observed session has no live spawn record: {spawn_id}")


def _get_or_create_observed_chat_locked(
    runtime_root: Path,
    source_chat_id: str,
    *,
    harness: str,
    native_store: str,
    session_id: str,
) -> str:
    """Materialize context, not a second live execution or a copy of its lineage.

    Both live starts and observations use SessionStartEvent/SessionSettings as
    their schema. Work is part of the initial event, not a later repair/update.
    The owning execution keeps its own entry chat and lifetime lease.
    """
    source = chat_record(runtime_root, source_chat_id)
    if source is None or source.record_mode != "live":
        raise ValueError(f"Source chat is missing or historical: {source_chat_id}")
    candidates: list[tuple[str, SessionRecord]] = []
    for chat in native_chats(runtime_root, session_id):
        records: dict[str, SessionRecord] = {}
        first_start: str | None = None
        for event in chat_rows(runtime_root, chat):
            project_session_event(records, event)
            if first_start is None and chat in records:
                first_start = records[chat].started_at
        if first_start is not None:
            candidates.append((first_start, records[chat]))
    # The old full fold visited aliases in first-start order, not the time of
    # their latest continuation. The source chat still takes precedence.
    candidates.sort(key=lambda item: (item[0], _session_sort_key(item[1].chat_id)))
    for record in (source, *(record for _, record in candidates)):
        if record.record_mode == "live" and (
            record.harness, record.native_store, record.harness_session_id
        ) == (harness, native_store, session_id):
            return record.chat_id
    chat_id = reserve_chat_id(runtime_root)
    generation = uuid.uuid4().hex
    now = utc_now_iso()
    settings = source.model_dump(include=set(SessionSettings.model_fields))
    settings["launch_policy_snapshot"] = get_session_launch_policy_snapshot(runtime_root, source)
    start = SessionStartEvent(
        chat_id=ChatId(chat_id), history_id=uuid.uuid4(), kind=source.kind,
        harness=harness, harness_session_id=HarnessSessionId(session_id),
        native_store=native_store, session_instance_id=generation, started_at=now,
        **settings,
    )
    for event in (
        start,
        SessionStopEvent(chat_id=ChatId(chat_id), session_instance_id=generation, stopped_at=now),
    ):
        append_chat_rows(runtime_root, chat_id, [event])
    return chat_id


def list_session_generations(runtime_root: Path) -> tuple[SessionRecord, ...]:
    """All generations, including historical and legacy starts, in source order."""
    return fold_session_generations(
        read_events(RuntimePaths.from_root_dir(runtime_root).sessions_jsonl, parse_event)
    )
