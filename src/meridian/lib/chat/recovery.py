"""Recovery helpers for persisted chat event logs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.chat.backend_acquisition import BackendAcquisition
from meridian.lib.chat.event_index import ChatEventIndex
from meridian.lib.chat.event_log import ChatEventLog
from meridian.lib.chat.protocol import (
    CHAT_EXITED,
    RUNTIME_ERROR,
    TURN_COMPLETED,
    TURN_STARTED,
    ChatEvent,
    utc_now_iso,
)
from meridian.lib.chat.runtime import LiveChatEntry, PersistedChatRecord, build_live_entry
from meridian.lib.state.paths import RuntimePaths


@dataclass(frozen=True)
class RecoveryResult:
    live_entries: dict[str, LiveChatEntry]
    persisted_only: dict[str, PersistedChatRecord]


def recover_all(
    *,
    paths: RuntimePaths,
    project_root: Path,
    backend_acquisition: BackendAcquisition,
    checkpoint_lock: asyncio.Lock,
    active_chat_counter: Callable[[], int],
) -> RecoveryResult:
    """Rebuild chat registries from persisted history files."""

    live_entries: dict[str, LiveChatEntry] = {}
    persisted_only: dict[str, PersistedChatRecord] = {}
    chats_dir = paths.chats_dir
    if not chats_dir.exists():
        return RecoveryResult(live_entries=live_entries, persisted_only=persisted_only)

    for history_path in chats_dir.glob("*/history.jsonl"):
        chat_id = history_path.parent.name
        event_log = ChatEventLog(history_path)
        events = list(event_log.read_all())
        if not events:
            continue

        event_index = ChatEventIndex(paths.chats_dir / chat_id / "index.sqlite3")
        event_index.rebuild_from_log(event_log)

        if any(event.type == CHAT_EXITED for event in events):
            persisted_only[chat_id] = PersistedChatRecord(
                event_log=event_log,
                event_index=event_index,
                state="closed",
            )
            continue

        entry = build_live_entry(
            chat_id=chat_id,
            event_log=event_log,
            event_index=event_index,
            backend_acquisition=backend_acquisition,
            project_root=project_root,
            checkpoint_lock=checkpoint_lock,
            active_chat_counter=active_chat_counter,
        )
        live_entries[chat_id] = entry
        last_state = _state_from_events(events)
        if last_state == "active":
            persisted = event_log.append(
                ChatEvent(
                    type=RUNTIME_ERROR,
                    seq=0,
                    chat_id=chat_id,
                    execution_id=_last_execution_id(events),
                    timestamp=utc_now_iso(),
                    payload={"reason": "backend_lost_after_restart"},
                )
            )
            event_index.upsert(persisted)

    return RecoveryResult(live_entries=live_entries, persisted_only=persisted_only)


def _state_from_events(events: list[ChatEvent]) -> str:
    state = "idle"
    for event in events:
        if event.type == TURN_STARTED:
            state = "active"
        elif event.type == TURN_COMPLETED:
            state = "idle"
        elif event.type == CHAT_EXITED:
            state = "closed"
        elif event.type == RUNTIME_ERROR:
            reason = event.payload.get("reason")
            if reason == "backend_lost_after_restart" and state == "active":
                state = "idle"
    return state


def _last_execution_id(events: list[ChatEvent]) -> str:
    for event in reversed(events):
        if event.execution_id:
            return event.execution_id
    return ""


__all__ = ["RecoveryResult", "recover_all"]
