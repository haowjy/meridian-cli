"""List recent primary sessions for interactive and plain browse surfaces."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.formatting import relative_time, tabular
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.reference_recovery import recover_recorded_chat_harness_session_ids
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.ops.session_reentry import Blocked, SessionReentryDecision, decide_reentry
from meridian.lib.state import session_store, work_store
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.idle_store import IdleState, IdleStore
from meridian.lib.state.paths import ProjectPaths, RuntimePaths, resolve_project_paths
from meridian.lib.state.spawn.model import SpawnRecord


class SessionListInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None
    limit: int = Field(default=50, gt=0)
    status: Literal["active", "archived"] = "active"
    sort: Literal["updated", "created"] = "updated"


class SessionListRow(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    chat_id: str
    history_id: str | None = None
    session_generation: str = ""
    archived: bool = False
    historical: bool = False
    created_at: str = ""
    harness: str = ""
    tmux_target: str | None = None
    last_turn_at: str = ""
    cache_deadline: float | None = None
    activity_at: str
    live: bool
    reentry: SessionReentryDecision
    agent: str
    model: str
    work_label: str
    task_cwd: str
    native_status: str = ""

    @property
    def stale(self) -> bool:
        return (
            not self.live and self.cache_deadline is not None and time.time() > self.cache_deadline
        )

    @property
    def filter_text(self) -> str:
        return " ".join(
            (
                self.chat_id,
                self.tmux_target or "",
                self.work_label,
                self.agent,
                self.model,
                self.task_cwd,
            )
        ).lower()


class SessionListOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    rows: tuple[SessionListRow, ...] = ()
    total_count: int = 0

    @property
    def older_count(self) -> int:
        return max(0, self.total_count - len(self.rows))

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not self.rows:
            return "no primary sessions"

        rows = [["LIVE", "STALE", "TMUX", "WORK", "AGENT", "MODEL", "AGE", "C-ID"]]
        rows.extend(
            [
                "●" if row.live else "",
                "⚠" if row.stale else "",
                row.tmux_target or "",
                row.work_label,
                row.agent,
                row.model,
                relative_time(row.last_turn_at or row.activity_at).removesuffix(" ago"),
                row.chat_id
                + (" [ZIP]" if row.archived else " [historical]" if row.historical else ""),
            ]
            for row in self.rows
        )
        output = tabular(rows)
        if self.older_count:
            output += f"\n({len(self.rows)} of {self.total_count} shown — use --limit to see more)"
        return output


# Estimated fallbacks when notification state is absent or has been garbage-collected.
_DEFAULT_CACHE_TTL_SECONDS = {"claude": 300, "codex": 1800, "opencode": 300, "pi": 300}


def _turn_metadata(
    harness: str,
    fallback: str,
    live: bool,
    idle: IdleState | None,
) -> tuple[str, float | None]:
    idle_at = idle.idle_since_ms / 1000 if idle and idle.idle_since_ms else None
    last_turn = datetime.fromtimestamp(idle_at, UTC).isoformat() if live and idle_at else fallback
    ttl = idle.ttl_seconds if idle and idle.ttl_seconds else _DEFAULT_CACHE_TTL_SECONDS.get(harness)
    if not fallback or ttl is None:
        return last_turn, None
    anchor = idle_at if idle_at is not None else datetime.fromisoformat(fallback).timestamp()
    return last_turn, anchor + ttl


def _work_label(runtime_root: Path, paths: ProjectPaths, work_id: str) -> str:
    item = work_store.get_work_item(runtime_root, work_id, paths=paths)
    return item.name if item is not None else work_id


def session_list_sync(
    payload: SessionListInput,
    ctx: RuntimeContext | None = None,
) -> SessionListOutput:
    """Materialize recent primary rows without opening transcripts."""

    _ = ctx
    roots = resolve_roots_for_read(payload.project_root)
    if roots is None:
        return SessionListOutput()

    sessions_dir = RuntimePaths.from_root_dir(roots.runtime_root).sessions_dir
    live_chat_ids = {
        path.name.removesuffix(".lease.json")
        for path in sessions_dir.glob("*.lease.json")
        if session_store.is_session_lease_owner_alive(
            roots.runtime_root, path.name.removesuffix(".lease.json")
        )
    }
    index = HistoryIndex(roots.runtime_root)
    visible_records, total_count = index.recent_sessions(
        limit=payload.limit,
        live_chat_ids=live_chat_ids,
        status=payload.status,
        sort=payload.sort,
    )
    archived_metadata = index.archived_session_metadata(
        {str(record.history_id) for record in visible_records if isinstance(record, SpawnRecord)}
    )
    identities: set[tuple[str, str]] = {
        (record.harness, record.harness_session_id)
        for record in visible_records
        if record.harness and record.harness_session_id
    }
    identities.update(
        (record.harness, record.harness_session_id)
        for record in archived_metadata.values()
        if record.harness_session_id
    )
    models = index.last_executed_models(identities)
    idle_store = IdleStore()
    idle_states = {key: idle_store.read(*key) for key in identities}

    recorded_harness_sessions = recover_recorded_chat_harness_session_ids(
        roots.runtime_root,
        [record for record in visible_records if isinstance(record, session_store.SessionRecord)],
    )
    work_ids = {
        record.work_id if isinstance(record, SpawnRecord) else record.active_work_id
        for record in visible_records
    }
    project_paths = resolve_project_paths(roots.project_root)
    work_labels = {
        work_id: _work_label(roots.project_state_dir, project_paths, work_id)
        for work_id in work_ids
        if work_id
    }

    rows: list[SessionListRow] = []
    for record in visible_records:
        if isinstance(record, SpawnRecord):
            ref = str(record.history_id)
            metadata = archived_metadata.get(ref)
            harness = metadata.harness if metadata else record.harness or ""
            native_id = metadata.harness_session_id if metadata else record.harness_session_id
            fallback = (
                (metadata.stopped_at or metadata.started_at)
                if metadata
                else record.terminal.finished_at
                if record.terminal
                else record.started_at or ""
            )
            last_turn, cache_deadline = _turn_metadata(
                harness,
                fallback,
                False,
                idle_states.get((harness, native_id or "")),
            )
            rows.append(
                SessionListRow(
                    chat_id=ref,
                    history_id=ref,
                    session_generation=record.session_instance_id or "",
                    archived=True,
                    last_turn_at=last_turn,
                    cache_deadline=cache_deadline,
                    created_at=(metadata.created_at or metadata.started_at)
                    if metadata
                    else record.started_at or "",
                    tmux_target=metadata.tmux_target if metadata else None,
                    harness=harness,
                    activity_at=record.terminal.finished_at
                    if record.terminal
                    else record.started_at or "",
                    live=False,
                    reentry=Blocked(f"archived history; read with meridian session log {ref}"),
                    agent=record.agent or "",
                    model=models.get((harness, native_id or ""), record.model or ""),
                    work_label=work_labels.get(record.work_id or "", ""),
                    task_cwd=record.task_cwd or record.execution_cwd or "",
                )
            )
            continue
        live = record.chat_id in live_chat_ids
        idle = idle_states.get((record.harness, record.harness_session_id or ""))
        fallback = record.stopped_at or record.started_at
        last_turn, cache_deadline = _turn_metadata(record.harness, fallback, live, idle)

        rows.append(
            SessionListRow(
                chat_id=record.chat_id,
                history_id=str(record.history_id) if record.history_id else None,
                session_generation=record.session_instance_id
                or record.harness_session_id
                or record.started_at,
                created_at=record.created_at or record.started_at,
                harness=record.harness or "",
                tmux_target=record.tmux_target,
                last_turn_at=last_turn,
                cache_deadline=cache_deadline,
                historical=record.record_mode == "historical",
                activity_at=record.stopped_at or record.started_at,
                live=live,
                reentry=Blocked("historical record; read-only, cannot resume or fork")
                if record.record_mode == "historical"
                else decide_reentry(
                    chat_id=record.chat_id,
                    live=live,
                    has_harness_session=(
                        record.chat_id in recorded_harness_sessions
                        and record.native_key() is not None
                    ),
                ),
                agent=record.agent,
                model=models.get((record.harness, record.harness_session_id or ""), record.model),
                work_label=work_labels.get(record.active_work_id or "", ""),
                task_cwd=record.task_cwd or record.execution_cwd or "",
                native_status="" if record.native_key() is not None else "unbound",
            )
        )

    return SessionListOutput(rows=tuple(rows), total_count=total_count)


session_list = async_from_sync(session_list_sync)


__all__ = [
    "SessionListInput",
    "SessionListOutput",
    "SessionListRow",
    "session_list",
    "session_list_sync",
]
