"""Session log operation with compaction-aware segment navigation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import async_from_sync
from meridian.lib.ops.session_read import read_session_transcript
from meridian.lib.ops.session_render import (
    paginate_segment,
    select_compaction_segment,
    showing_window,
)


class SessionLogInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str = ""
    compaction: int = 0
    last_n: int | None = None
    offset: int = 0
    file_path: str | None = None
    project_root: str | None = None


class SessionLogMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    role: str
    content: str


class SessionLogOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    total_compactions: int
    segment: int
    segment_messages: int
    showing: str
    messages: tuple[SessionLogMessage, ...]
    has_newer: bool = False
    has_older: bool = False
    has_earlier_segments: bool = False
    source: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        message_label = "message" if self.segment_messages == 1 else "messages"
        source = f" ({self.source})" if self.source else ""
        lines = [
            f"Session {self.session_id}{source} — segment {self.segment}, "
            f"{self.segment_messages} {message_label} (showing {self.showing})"
        ]
        for message in self.messages:
            lines.append("")
            lines.append(f"--- {message.index} [{message.role}] ---")
            lines.append(message.content)
        hints: list[str] = []
        if self.has_older:
            hints.append("Use --last N to show more messages.")
        if self.has_newer:
            hints.append("Use --offset N to page forward.")
        if self.has_earlier_segments:
            hints.append("Use -c N for earlier compaction segments.")
        if hints:
            lines.append("")
            lines.extend(hints)
        return "\n".join(lines)


def session_log_sync(
    payload: SessionLogInput,
    ctx: RuntimeContext | None = None,
) -> SessionLogOutput:
    _ = ctx
    read = read_session_transcript(
        ref=payload.ref,
        file_path=payload.file_path,
        project_root=payload.project_root,
    )
    segment_messages = select_compaction_segment(read.segments, compaction=payload.compaction)
    page = paginate_segment(
        segment_messages,
        last_n=payload.last_n,
        offset=payload.offset,
    )

    output_messages = tuple(
        SessionLogMessage(index=page.start_index + idx + 1, role=item.role, content=item.content)
        for idx, item in enumerate(page.messages)
    )

    return SessionLogOutput(
        session_id=read.target.session_id,
        total_compactions=read.total_compactions,
        segment=payload.compaction,
        segment_messages=len(segment_messages),
        showing=showing_window(page.start_index, len(output_messages)),
        messages=output_messages,
        has_newer=page.has_newer,
        has_older=page.has_older,
        has_earlier_segments=read.total_compactions > payload.compaction,
        source=read.target.source,
    )


session_log = async_from_sync(session_log_sync)


__all__ = [
    "SessionLogInput",
    "SessionLogMessage",
    "SessionLogOutput",
    "session_log",
    "session_log_sync",
]
