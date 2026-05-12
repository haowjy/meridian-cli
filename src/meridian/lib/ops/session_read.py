"""Read-only session transcript loading boundary for session surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.harness.transcript import TranscriptMessage, parse_transcript_file
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.ops.session_target import SessionLogTarget, resolve_session_log_target


class SessionTranscriptRead(NamedTuple):
    project_root: Path
    runtime_root: Path
    target: SessionLogTarget
    segments: list[list[TranscriptMessage]]
    total_compactions: int


def read_session_transcript(
    *,
    ref: str,
    file_path: str | None,
    project_root: str | None,
) -> SessionTranscriptRead:
    explicit_project_root = Path(project_root).expanduser().resolve() if project_root else None
    resolved_project_root = resolve_project_root_resolution(explicit_project_root).project_root
    runtime_root = resolve_runtime_root_for_read(resolved_project_root)
    target = resolve_session_log_target(
        ref=ref,
        file_path=file_path,
        project_root=resolved_project_root,
        runtime_root=runtime_root,
    )
    segments, total_compactions = parse_transcript_file(target.file_path)
    return SessionTranscriptRead(
        project_root=resolved_project_root,
        runtime_root=runtime_root,
        target=target,
        segments=segments,
        total_compactions=total_compactions,
    )


__all__ = [
    "SessionTranscriptRead",
    "read_session_transcript",
]
