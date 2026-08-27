"""List recent primary sessions for interactive and plain browse surfaces."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.formatting import relative_time, tabular
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.reference_recovery import recover_recorded_chat_harness_session_ids
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.ops.session_reentry import SessionReentryDecision, decide_reentry
from meridian.lib.state import session_store, work_store
from meridian.lib.state.paths import RuntimePaths


class SessionListInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None
    limit: int = Field(default=50, gt=0)


def _chat_recency_key(chat_id: str) -> tuple[int, str]:
    suffix = chat_id[1:] if chat_id.startswith("c") else ""
    return (int(suffix), chat_id) if suffix.isdigit() else (-1, chat_id)


class SessionListRow(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    chat_id: str
    activity_at: str
    live: bool
    reentry: SessionReentryDecision
    agent: str
    model: str
    work_label: str
    task_cwd: str

    @property
    def filter_text(self) -> str:
        return " ".join(
            (self.chat_id, self.work_label, self.agent, self.model, self.task_cwd)
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

        rows = [["C-ID", "AGE", "LIVE", "AGENT", "MODEL", "WORK"]]
        rows.extend(
            [
                row.chat_id,
                relative_time(row.activity_at).removesuffix(" ago"),
                "●" if row.live else "",
                row.agent or "—",
                row.model or "—",
                row.work_label or "—",
            ]
            for row in self.rows
        )
        output = tabular(rows)
        if self.older_count:
            output += (
                f"\n({len(self.rows)} of {self.total_count} shown — "
                "use --limit to see more)"
            )
        return output


def _work_label(runtime_root: Path, work_id: str | None) -> str:
    if not work_id:
        return ""
    item = work_store.get_work_item(runtime_root, work_id)
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

    records = [
        record
        for record in session_store.list_all_session_records(roots.runtime_root)
        if record.kind == "primary"
    ]
    sessions_dir = RuntimePaths.from_root_dir(roots.runtime_root).sessions_dir
    lease_chat_ids = {
        path.name.removesuffix(".lease.json")
        for path in sessions_dir.glob("*.lease.json")
    }
    live_chat_ids = {
        record.chat_id
        for record in records
        if record.chat_id in lease_chat_ids
        and session_store.is_session_lease_owner_alive(roots.runtime_root, record.chat_id)
    }
    records.sort(
        key=lambda record: (
            record.chat_id in live_chat_ids,
            record.stopped_at or record.started_at,
            _chat_recency_key(record.chat_id),
        ),
        reverse=True,
    )
    visible_records = records[: payload.limit]
    recorded_harness_sessions = recover_recorded_chat_harness_session_ids(
        roots.runtime_root,
        visible_records,
    )
    work_labels = {
        work_id: _work_label(roots.project_state_dir, work_id)
        for work_id in {
            record.active_work_id for record in visible_records if record.active_work_id
        }
    }

    rows: list[SessionListRow] = []
    for record in visible_records:
        live = record.chat_id in live_chat_ids
        rows.append(
            SessionListRow(
                chat_id=record.chat_id,
                activity_at=record.stopped_at or record.started_at,
                live=live,
                reentry=decide_reentry(
                    chat_id=record.chat_id,
                    live=live,
                    has_harness_session=record.chat_id in recorded_harness_sessions,
                ),
                agent=record.agent,
                model=record.model,
                work_label=work_labels.get(record.active_work_id or "", ""),
                task_cwd=record.task_cwd or record.execution_cwd or "",
            )
        )

    return SessionListOutput(rows=tuple(rows), total_count=len(records))


session_list = async_from_sync(session_list_sync)


__all__ = [
    "SessionListInput",
    "SessionListOutput",
    "SessionListRow",
    "session_list",
    "session_list_sync",
]
