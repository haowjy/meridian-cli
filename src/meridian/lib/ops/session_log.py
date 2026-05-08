"""Session log operation with compaction-aware segment navigation."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.project_root import resolve_project_root
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.util import FormatContext
from meridian.lib.harness.transcript import (
    DefaultTranscriptEventParser,
    TranscriptMessage,
    parse_transcript_file,
)
from meridian.lib.ops.runtime import async_from_sync, resolve_runtime_root_for_read
from meridian.lib.ops.session_target import (
    SessionLogTarget,
    resolve_session_log_target,
    spawn_output_path_for_target,
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


def _extract_from_event(payload: dict[str, object]) -> tuple[list[TranscriptMessage], bool]:
    """Compatibility shim for parser tests during the Phase 7 split."""

    return DefaultTranscriptEventParser().parse(payload)


def parse_session_file(path: Path) -> tuple[list[list[TranscriptMessage]], int]:
    """Compatibility shim: parse with extracted transcript providers/parsers."""

    return parse_transcript_file(path)


def _spawn_output_path(runtime_root: Path, spawn_id: str, *, live_first: bool) -> Path | None:
    return spawn_output_path_for_target(runtime_root, spawn_id, live_first=live_first)


def resolve_target(
    payload: SessionLogInput, *, project_root: Path, runtime_root: Path
) -> SessionLogTarget:
    return resolve_session_log_target(
        ref=payload.ref,
        file_path=payload.file_path,
        project_root=project_root,
        runtime_root=runtime_root,
    )


def _select_segment(
    segments: list[list[TranscriptMessage]],
    *,
    compaction: int,
) -> list[TranscriptMessage]:
    if compaction < 0:
        raise ValueError("compaction must be >= 0")

    segment_index = len(segments) - 1 - compaction
    if segment_index < 0:
        raise ValueError(
            f"Compaction segment {compaction} out of range (available: 0-{len(segments) - 1})"
        )
    return segments[segment_index]


def _paginate_messages(
    messages: list[TranscriptMessage],
    *,
    last_n: int | None,
    offset: int,
) -> tuple[list[TranscriptMessage], int]:
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if last_n is not None and last_n < 0:
        raise ValueError("last_n must be >= 0")

    total = len(messages)
    if offset >= total:
        return ([], total)

    end = total - offset
    start = 0 if last_n is None else max(end - last_n, 0)

    return (messages[start:end], start)


def _showing_window(messages: tuple[SessionLogMessage, ...]) -> str:
    if not messages:
        return "0-0"
    return f"{messages[0].index}-{messages[-1].index}"


def session_log_sync(
    payload: SessionLogInput,
    ctx: RuntimeContext | None = None,
) -> SessionLogOutput:
    _ = ctx
    explicit_project_root = (
        Path(payload.project_root).expanduser().resolve() if payload.project_root else None
    )
    project_root = resolve_project_root(explicit_project_root)
    runtime_root = resolve_runtime_root_for_read(project_root)

    target = resolve_target(payload, project_root=project_root, runtime_root=runtime_root)
    segments, total_compactions = parse_transcript_file(target.file_path)
    segment_messages = _select_segment(segments, compaction=payload.compaction)

    selected, start_index = _paginate_messages(
        segment_messages,
        last_n=payload.last_n,
        offset=payload.offset,
    )

    output_messages = tuple(
        SessionLogMessage(index=start_index + idx + 1, role=item.role, content=item.content)
        for idx, item in enumerate(selected)
    )

    return SessionLogOutput(
        session_id=target.session_id,
        total_compactions=total_compactions,
        segment=payload.compaction,
        segment_messages=len(segment_messages),
        showing=_showing_window(output_messages),
        messages=output_messages,
        has_newer=payload.offset > 0,
        has_older=start_index > 0,
        has_earlier_segments=total_compactions > payload.compaction,
        source=target.source,
    )


session_log = async_from_sync(session_log_sync)


__all__ = [
    "SessionLogInput",
    "SessionLogMessage",
    "SessionLogOutput",
    "parse_session_file",
    "resolve_target",
    "session_log",
    "session_log_sync",
]
