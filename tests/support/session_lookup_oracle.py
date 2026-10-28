"""Frozen lookup oracle from stacked base 9db5fbfb6 for #418's keyed reads.

Disk reads may be substituted with a captured corpus by the real-copy runner;
Do not update its lookup algorithms or global event order from the keyed-read
implementation under test.
"""

import json
import re
import uuid
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ValidationError

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.core.types import (
    ChatId,
    HarnessSessionId,
)
from meridian.lib.platform.locking import (
    lock_file,
)
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import read_events, utc_now_iso
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.jsonl import append_durable_jsonl_line
from meridian.lib.state.paths import RuntimePaths
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
    UnsupportedSessionRecords,
    fold_session_generations,
    is_historical_generation,
    parse_event,
    validate_historical_import,
    validate_startup_identity,
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
from meridian.lib.state.session_store import (
    get_session_launch_policy_snapshot,
    reserve_chat_id,
)
from meridian.lib.state.spawn.model import SpawnRecord

_SESSION_CHAT_ID_FIELD = re.compile(rb'"chat_id"\s*:\s*"((?:[^"\\]|\\.)*)"')

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


def append_event(
    data_path: Path,
    lock_path: Path,
    event: BaseModel,
    *,
    exclude_none: bool = False,
) -> None:
    line = json.dumps(
        event.model_dump(mode="json", exclude_none=exclude_none),
        separators=(",", ":"),
        sort_keys=True,
    )
    with lock_file(lock_path):
        append_durable_jsonl_line(data_path, line)


def _append_session_event(
    data_path: Path,
    lock_path: Path,
    event: BaseModel,
    *,
    exclude_none: bool = False,
) -> None:
    changes = HistoryChanges(data_path.parent)
    with lock_file(changes.mutation_lock, mode="shared"), lock_file(lock_path):
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
        append_event(data_path, lock_path, event, exclude_none=exclude_none)


def _parse_model_observation(payload: Mapping[str, object]) -> SessionModelObservationEvent | None:
    if payload.get("event") != "model_observation":
        return None
    try:
        return SessionModelObservationEvent.model_validate(payload)
    except ValidationError:
        return None


def read_session_events(
    runtime_root: Path, chat_ids: Collection[str] | None = None
) -> list[SessionEvent]:
    """Parse every session event in source order, or only those of ``chat_ids``.

    One chat's projection reads no other chat's events, so folding the selected
    events equals folding all of them for those chats.
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
                if isinstance(record, dict)
                else None
            )
            if not isinstance(raw_id, str) or raw_id.strip() not in chat_ids:
                return None
        return parse_event(payload, UnsupportedSessionRecords())

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


def get_session_record(runtime_root: Path, chat_id: str | None) -> SessionRecord | None:
    """Return a materialized record for one chat ID, if present."""

    if chat_id is None:
        return None
    return _records_by_session(runtime_root, chat_ids={chat_id}).get(chat_id)


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
    paths = RuntimePaths.from_root_dir(runtime_root)
    events = read_events(
        paths.sessions_jsonl,
        lambda payload: parse_event(payload, UnsupportedSessionRecords()),
    )
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
        read_events(
            paths.sessions_jsonl,
            lambda payload: parse_event(payload, UnsupportedSessionRecords()),
        )
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
            events = read_events(
                paths.sessions_jsonl,
                lambda payload: parse_event(payload, UnsupportedSessionRecords()),
            )
            source_start = next(
                (
                    start
                    for start in events
                    if isinstance(start, SessionStartEvent)
                    and start.chat_id == event.chat_id
                    and start.session_instance_id == event.session_instance_id
                    and start.harness == event.harness
                ),
                None,
            )
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
                if (
                    native_id is not None
                    and prior_id == native_id
                    and (
                        event.kind == "initial_seed"
                        or (prior.kind == "invocation_started" and prior.spawn_id == event.spawn_id)
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
                        append_event(
                            paths.sessions_jsonl,
                            paths.sessions_flock,
                            SessionUpdateEvent(
                                chat_id=event.chat_id,
                                session_instance_id=event.session_instance_id,
                                startup_attempt_id=event.startup_attempt_id,
                                harness_session_id=HarnessSessionId(native_id),
                            ),
                            exclude_none=True,
                        )
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
    if len({key for record in matches if (key := record.native_key()) is not None}) > 1:
        raise NativeSessionUnavailable(normalized, "ambiguous_native_file")
    return max(matches, key=lambda item: (item.started_at, _session_sort_key(item.chat_id)))


def _linked_spawn_policy(runtime_root: Path, spawn_id: str | None) -> LaunchPolicySnapshot | None:
    """Bounded legacy read: an exact recorded link, never a newest-owner guess."""
    from meridian.lib.state import spawn_store

    row = spawn_store.get_spawn(runtime_root, spawn_id) if spawn_id else None
    return row.launch_policy_snapshot if row is not None else None


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
    paths = RuntimePaths.from_root_dir(runtime_root)
    records = _records_by_session(runtime_root)
    source = records.get(source_chat_id)
    if source is None or source.record_mode != "live":
        raise ValueError(f"Source chat is missing or historical: {source_chat_id}")
    for record in (source, *records.values()):
        if record.record_mode == "live" and (
            record.harness,
            record.native_store,
            record.harness_session_id,
        ) == (harness, native_store, session_id):
            return record.chat_id
    chat_id = reserve_chat_id(runtime_root)
    generation = uuid.uuid4().hex
    now = utc_now_iso()
    settings = source.model_dump(include=set(SessionSettings.model_fields))
    settings["launch_policy_snapshot"] = get_session_launch_policy_snapshot(runtime_root, source)
    start = SessionStartEvent(
        chat_id=ChatId(chat_id),
        history_id=uuid.uuid4(),
        kind=source.kind,
        harness=harness,
        harness_session_id=HarnessSessionId(session_id),
        native_store=native_store,
        session_instance_id=generation,
        started_at=now,
        **settings,
    )
    for event in (
        start,
        SessionStopEvent(chat_id=ChatId(chat_id), session_instance_id=generation, stopped_at=now),
    ):
        _append_session_event(paths.sessions_jsonl, paths.sessions_flock, event)
    return chat_id


def list_session_generations(runtime_root: Path) -> tuple[SessionRecord, ...]:
    """All generations, including historical and legacy starts, in source order."""
    return fold_session_generations(
        read_events(
            RuntimePaths.from_root_dir(runtime_root).sessions_jsonl,
            lambda payload: parse_event(payload, UnsupportedSessionRecords()),
        )
    )


def session_records_for_spawns(
    root: Path, records: Iterable[SpawnRecord]
) -> dict[str, SessionRecord]:
    """Return unmodified authoritative generations; ambiguous linkage is a conflict.

    Do not fill nullable identity fields here: capture fingerprints, historical
    provenance and publication witnesses must detect changes to those fields too.
    """
    generations = list_session_generations(root)
    linked: dict[str, list[SessionRecord]] = {}
    for session in generations:
        if session.spawn_id:
            linked.setdefault(session.spawn_id, []).append(session)
    exact = {(session.chat_id, session.session_instance_id): session for session in generations}
    result: dict[str, SessionRecord] = {}
    for record in records:
        candidates = linked.get(record.id, [])
        if record.session_instance_id and record.chat_id is not None:
            session = exact.get((record.chat_id, record.session_instance_id))
            if session is None and candidates:
                raise ValueError(f"Session generation conflicts with history: {record.id}")
            candidates = [session] if session is not None else []
        if len(candidates) > 1:
            raise ValueError(f"Ambiguous session metadata for history: {record.id}")
        if candidates:
            session = candidates[0]
            if session.spawn_id not in {None, record.id} or session.history_id not in {
                None,
                record.history_id,
            }:
                raise ValueError(f"Session identity conflicts with history: {record.id}")
            result[record.id] = session
    return result
