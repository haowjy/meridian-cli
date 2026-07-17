"""File-backed session tracking for a Meridian state root's `sessions.jsonl`."""

import json
import os
import uuid
from pathlib import Path
from typing import IO, Any, Literal, NamedTuple, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.platform.locking import (
    acquire_file_lock,
    lock_file,
    release_file_lock,
    try_lock_file,
)
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import append_event, read_events, utc_now_iso
from meridian.lib.state.liveness import is_process_alive
from meridian.lib.state.paths import RuntimePaths


class _SessionLockHandles(NamedTuple):
    session: IO[bytes]
    project_lifetime: IO[bytes]
    session_instance_id: str


_SESSION_LOCK_HANDLES: dict[tuple[Path, str], _SessionLockHandles] = {}


class SessionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat_id: str
    kind: Literal["primary", "spawn"]
    harness: str
    harness_session_id: str
    control_root: str | None = None
    task_cwd: str | None = None
    execution_cwd: str | None = None
    claude_config_dir: str | None = None
    harness_session_ids: tuple[str, ...]
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
    forked_from_chat_id: str | None = None
    spawn_id: str | None = None


class SessionStartEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["start"] = "start"
    chat_id: str
    kind: Literal["primary", "spawn"] = "spawn"
    harness: str
    harness_session_id: str
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
    forked_from_chat_id: str | None = None
    spawn_id: str | None = None


class SessionStopEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["stop"] = "stop"
    chat_id: str
    session_instance_id: str = ""
    stopped_at: str | None = None


class SessionUpdateEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["update"] = "update"
    chat_id: str
    harness_session_id: str
    session_instance_id: str = ""
    claude_config_dir: str | None = None
    active_work_id: str | None = None


type SessionEvent = SessionStartEvent | SessionStopEvent | SessionUpdateEvent
type MaterializedCleanupScope = str


class StaleSessionCleanup(NamedTuple):
    cleaned_ids: tuple[str, ...]
    materialized_scopes: tuple[MaterializedCleanupScope, ...]


def _parse_event(payload: dict[str, Any]) -> SessionEvent | None:
    event_type = payload.get("event")
    try:
        if event_type == "start":
            return SessionStartEvent.model_validate(payload)
        if event_type == "stop":
            return SessionStopEvent.model_validate(payload)
        if event_type == "update":
            return SessionUpdateEvent.model_validate(payload)
    except ValidationError:
        return None
    return None


def _record_from_start_event(event: SessionStartEvent) -> SessionRecord:
    return SessionRecord(
        chat_id=event.chat_id,
        kind=event.kind,
        harness=event.harness,
        harness_session_id=event.harness_session_id,
        control_root=event.control_root,
        task_cwd=event.task_cwd,
        execution_cwd=event.execution_cwd,
        claude_config_dir=event.claude_config_dir,
        harness_session_ids=(event.harness_session_id,),
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


def _read_session_lease_data(paths: RuntimePaths, chat_id: str) -> tuple[bool, str, int | None]:
    lease_path = _session_lease_path(paths, chat_id)
    if not lease_path.is_file():
        return (False, "", None)
    try:
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return (False, "", None)
    if not isinstance(payload, dict):
        return (False, "", None)
    payload_dict = cast("dict[str, Any]", payload)
    generation = payload_dict.get("session_instance_id")
    owner_pid = payload_dict.get("owner_pid")
    parsed_owner_pid = (
        owner_pid if isinstance(owner_pid, int) and not isinstance(owner_pid, bool) else None
    )
    if isinstance(generation, str):
        return (True, generation, parsed_owner_pid)
    return (True, "", parsed_owner_pid)


def _read_session_lease(paths: RuntimePaths, chat_id: str) -> tuple[bool, str]:
    lease_exists, generation, _owner_pid = _read_session_lease_data(paths, chat_id)
    return (lease_exists, generation)


def _write_session_lease(paths: RuntimePaths, chat_id: str, session_instance_id: str) -> None:
    payload = {
        "chat_id": chat_id,
        "owner_pid": os.getpid(),
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


def _records_by_session(runtime_root: Path) -> dict[str, SessionRecord]:
    paths = RuntimePaths.from_root_dir(runtime_root)
    records: dict[str, SessionRecord] = {}

    for event in read_events(paths.sessions_jsonl, _parse_event):
        if isinstance(event, SessionStartEvent):
            record = _record_from_start_event(event)
            records[record.chat_id] = record
            continue
        if isinstance(event, SessionStopEvent):
            existing = records.get(event.chat_id)
            if existing is None:
                continue
            if not _generation_matches(existing.session_instance_id, event.session_instance_id):
                continue
            records[event.chat_id] = existing.model_copy(
                update={
                    "stopped_at": event.stopped_at
                    if event.stopped_at is not None
                    else existing.stopped_at,
                    "session_instance_id": event.session_instance_id
                    or existing.session_instance_id,
                }
            )
            continue
        existing = records.get(event.chat_id)
        if existing is None:
            continue
        if not _generation_matches(existing.session_instance_id, event.session_instance_id):
            continue
        session_ids = existing.harness_session_ids
        harness_session_id = existing.harness_session_id
        updated_work_id = existing.active_work_id
        claude_config_dir = existing.claude_config_dir
        session_instance_id = existing.session_instance_id
        normalized_harness_session_id = event.harness_session_id.strip()
        if normalized_harness_session_id:
            if normalized_harness_session_id not in session_ids:
                session_ids = (*session_ids, normalized_harness_session_id)
            harness_session_id = normalized_harness_session_id
        if event.session_instance_id.strip():
            session_instance_id = event.session_instance_id
        if event.active_work_id is not None:
            normalized_work_id = event.active_work_id.strip()
            updated_work_id = normalized_work_id or None
        if event.claude_config_dir is not None:
            normalized_config_dir = event.claude_config_dir.strip()
            claude_config_dir = normalized_config_dir or None
        records[event.chat_id] = existing.model_copy(
            update={
                "harness_session_id": harness_session_id,
                "harness_session_ids": session_ids,
                "session_instance_id": session_instance_id,
                "active_work_id": updated_work_id,
                "claude_config_dir": claude_config_dir,
            }
        )
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
    control_root: str | None = None,
    task_cwd: str | None = None,
    execution_cwd: str | None = None,
    claude_config_dir: str | None = None,
    kind: Literal["primary", "spawn"] = "spawn",
    spawn_id: str | None = None,
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
            chat_id=resolved_chat_id,
            kind=kind,
            harness=harness,
            harness_session_id=harness_session_id,
            control_root=control_root,
            task_cwd=task_cwd,
            execution_cwd=execution_cwd,
            claude_config_dir=claude_config_dir,
            model=model,
            agent=agent,
            agent_path=agent_path,
            skills=skills,
            skill_paths=skill_paths,
            params=params,
            session_instance_id=session_instance_id,
            started_at=started_at,
            forked_from_chat_id=forked_from_chat_id,
            spawn_id=spawn_id,
        )
        with lock_file(paths.sessions_flock):
            append_event(paths.sessions_jsonl, paths.sessions_flock, event)
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
        chat_id=chat_id,
        session_instance_id=session_instance_id,
        stopped_at=utc_now_iso(),
    )
    with lock_file(paths.sessions_flock):
        append_event(
            paths.sessions_jsonl,
            paths.sessions_flock,
            event,
            exclude_none=True,
        )
        _session_lease_path(paths, chat_id).unlink(missing_ok=True)
    _release_session_lock(runtime_root, chat_id)


def update_session_harness_id(runtime_root: Path, chat_id: str, harness_session_id: str) -> None:
    """Append a session update event carrying the resolved harness session ID."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    event = SessionUpdateEvent(
        chat_id=chat_id,
        harness_session_id=harness_session_id,
        session_instance_id=_session_instance_for_event(paths, runtime_root, chat_id),
    )
    append_event(
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
        chat_id=chat_id,
        harness_session_id="",
        session_instance_id=_session_instance_for_event(paths, runtime_root, chat_id),
        active_work_id=normalized_work_id,
    )
    append_event(
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
        chat_id=chat_id,
        harness_session_id="",
        session_instance_id=_session_instance_for_event(paths, runtime_root, chat_id),
        claude_config_dir=claude_config_dir,
    )
    append_event(
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
        _exists, _generation, owner_pid = _read_session_lease_data(paths, chat_id)
        if owner_pid is not None and is_process_alive(owner_pid):
            return True
    return False


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


def chat_ids_ever_attached_to_work(runtime_root: Path, work_id: str) -> set[str]:
    """Return session IDs ever attached to a work item in raw session events."""

    normalized_work_id = work_id.strip()
    if not normalized_work_id:
        return set()

    paths = RuntimePaths.from_root_dir(runtime_root)

    def _parse_work_attachment(payload: dict[str, Any]) -> str | None:
        event_type = payload.get("event")
        if event_type not in {"start", "update"}:
            return None
        chat_id = payload.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id.strip():
            return None
        active_work_id = payload.get("active_work_id")
        if not isinstance(active_work_id, str):
            return None
        if active_work_id.strip() != normalized_work_id:
            return None
        return chat_id.strip()

    return set(read_events(paths.sessions_jsonl, _parse_work_attachment))


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


def resolve_session_ref(runtime_root: Path, ref: str) -> SessionRecord | None:
    """Resolve session reference by harness session ID."""

    normalized = ref.strip()
    if not normalized:
        return None

    records = _records_by_session(runtime_root)
    matches = [record for record in records.values() if normalized in record.harness_session_ids]
    if not matches:
        return None
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

    stale: list[tuple[str, Path, IO[bytes]]] = []
    for lock_path in paths.sessions_dir.glob("*.lock"):
        chat_id = lock_path.stem
        try:
            handle = acquire_file_lock(lock_path, timeout=0)
        except TimeoutError:
            continue
        stale.append((chat_id, lock_path, handle))

    if not stale:
        return StaleSessionCleanup(cleaned_ids=(), materialized_scopes=())

    cleaned_ids: list[str] = []
    stale_cleanup_scopes: list[str] = []
    with lock_file(paths.sessions_flock):
        records = _records_by_session(runtime_root)
        stopped_at = utc_now_iso()
        for chat_id, _lock_path, _ in stale:
            existing = records.get(chat_id)
            lease_exists, lease_session_instance_id, lease_owner_pid = _read_session_lease_data(
                paths, chat_id
            )
            if (
                existing is not None
                and existing.kind == "primary"
                and lease_exists
                and lease_owner_pid is not None
                and is_process_alive(lease_owner_pid)
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
                append_event(
                    paths.sessions_jsonl,
                    paths.sessions_flock,
                    SessionStopEvent(
                        chat_id=chat_id,
                        session_instance_id=stop_session_instance_id,
                        stopped_at=stopped_at,
                    ),
                    exclude_none=True,
                )
                records[chat_id] = existing.model_copy(update={"stopped_at": stopped_at})

            should_clean = existing is None or existing.stopped_at is not None or not lease_exists
            if existing is not None and existing.stopped_at is None:
                should_clean = not lease_exists or _generation_matches(
                    existing.session_instance_id, lease_session_instance_id
                )
            if not should_clean:
                continue

            if existing is not None and existing.harness.strip():
                stale_cleanup_scopes.append(existing.harness.strip())
            cleaned_ids.append(chat_id)

    # Lock paths are stable coordination identities and are never removed.
    for chat_id, _lock_path, handle in stale:
        release_file_lock(handle)
        if chat_id in cleaned_ids:
            _SESSION_LOCK_HANDLES.pop(_session_lock_key(runtime_root, chat_id), None)

    for chat_id, _lock_path, _ in stale:
        if chat_id in cleaned_ids:
            _session_lease_path(paths, chat_id).unlink(missing_ok=True)

    return StaleSessionCleanup(
        cleaned_ids=tuple(sorted(cleaned_ids, key=_session_sort_key)),
        materialized_scopes=tuple(sorted(set(stale_cleanup_scopes))),
    )
