"""List recent primary sessions for interactive and plain browse surfaces."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.formatting import relative_time, tabular
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.ops.session_reentry import SessionReentryDecision, decide_reentry
from meridian.lib.state import session_store, work_store
from meridian.lib.state.paths import RuntimePaths


class SessionListInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None
    limit: int = Field(default=50, gt=0)


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
            output += f"\n+{self.older_count} older · raise --limit to see more"
        return output


def _work_label(runtime_root: Path, work_id: str | None) -> str:
    if not work_id:
        return ""
    item = work_store.get_work_item(runtime_root, work_id)
    return item.name if item is not None else work_id


def _has_harness_session(record: session_store.SessionRecord) -> bool:
    return any(value.strip() for value in record.harness_session_ids) or bool(
        (record.harness_session_id or "").strip()
    )


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

    rows: list[SessionListRow] = []
    for record in records:
        live = record.chat_id in lease_chat_ids and session_store.is_session_lease_owner_alive(
            roots.runtime_root, record.chat_id
        )
        rows.append(
            SessionListRow(
                chat_id=record.chat_id,
                activity_at=record.stopped_at or record.started_at,
                live=live,
                reentry=decide_reentry(
                    chat_id=record.chat_id,
                    live=live,
                    has_harness_session=_has_harness_session(record),
                ),
                agent=record.agent,
                model=record.model,
                work_label=_work_label(roots.project_state_dir, record.active_work_id),
                task_cwd=record.task_cwd or record.execution_cwd or "",
            )
        )

    rows.sort(key=lambda row: (row.live, row.activity_at), reverse=True)
    return SessionListOutput(rows=tuple(rows[: payload.limit]), total_count=len(rows))


session_list = async_from_sync(session_list_sync)


__all__ = [
    "SessionListInput",
    "SessionListOutput",
    "SessionListRow",
    "session_list",
    "session_list_sync",
]
