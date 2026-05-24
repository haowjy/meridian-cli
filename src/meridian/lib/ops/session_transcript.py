"""Canonical session transcript read + flatten boundary for session surfaces."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Literal, NamedTuple

from meridian.lib.harness.transcript import TranscriptMessage, parse_transcript_file
from meridian.lib.ops.runtime import resolve_runtime_authority_for_read
from meridian.lib.ops.session_target import SessionLogTarget, resolve_session_log_target


class AbsoluteTranscriptMessage(NamedTuple):
    ordinal: int
    segment_index: int
    segment_message_index: int
    role: str
    content: str


class SessionLogRoute(NamedTuple):
    mode: Literal["ref", "file"]
    value: str


class ParsedSessionTranscript(NamedTuple):
    project_root: Path
    runtime_root: Path
    target: SessionLogTarget
    route: SessionLogRoute
    segments: list[list[TranscriptMessage]]
    total_compactions: int
    messages: tuple[AbsoluteTranscriptMessage, ...]


def flatten_transcript_segments(
    segments: list[list[TranscriptMessage]],
) -> tuple[AbsoluteTranscriptMessage, ...]:
    flattened: list[AbsoluteTranscriptMessage] = []
    ordinal = 1
    for segment_index, segment_messages in enumerate(segments):
        for segment_message_index, message in enumerate(segment_messages, start=1):
            flattened.append(
                AbsoluteTranscriptMessage(
                    ordinal=ordinal,
                    segment_index=segment_index,
                    segment_message_index=segment_message_index,
                    role=message.role,
                    content=message.content,
                )
            )
            ordinal += 1
    return tuple(flattened)


def _route_from_request(
    *,
    ref: str,
    file_path: str | None,
    target: SessionLogTarget,
) -> SessionLogRoute:
    normalized_file = (file_path or "").strip()
    if normalized_file:
        return SessionLogRoute(mode="file", value=normalized_file)
    normalized_ref = ref.strip() or target.session_id
    return SessionLogRoute(mode="ref", value=normalized_ref)


def route_for_corpus_target(target: SessionLogTarget) -> SessionLogRoute:
    return SessionLogRoute(mode="file", value=target.file_path.as_posix())


def parse_session_target(
    *,
    project_root: Path,
    runtime_root: Path,
    target: SessionLogTarget,
    route: SessionLogRoute,
) -> ParsedSessionTranscript:
    segments, total_compactions = parse_transcript_file(target.file_path)
    return ParsedSessionTranscript(
        project_root=project_root,
        runtime_root=runtime_root,
        target=target,
        route=route,
        segments=segments,
        total_compactions=total_compactions,
        messages=flatten_transcript_segments(segments),
    )


def read_session_transcript(
    *,
    ref: str,
    file_path: str | None,
    project_root: str | None,
) -> ParsedSessionTranscript:
    authority = resolve_runtime_authority_for_read(project_root)
    runtime_root = authority.runtime_root or authority.project_state_dir
    target = resolve_session_log_target(
        ref=ref,
        file_path=file_path,
        project_root=authority.project_root,
        runtime_root=runtime_root,
    )
    route = _route_from_request(ref=ref, file_path=file_path, target=target)
    return parse_session_target(
        project_root=authority.project_root,
        runtime_root=runtime_root,
        target=target,
        route=route,
    )


def build_session_log_command(
    route: SessionLogRoute,
    *,
    from_ordinal: int | None = None,
    limit: int | None = None,
    around_ordinal: int | None = None,
    context: int | None = None,
) -> str:
    if route.mode == "file":
        base = f"meridian session log --file {shlex.quote(route.value)}"
    else:
        base = f"meridian session log {shlex.quote(route.value)}"

    if from_ordinal is not None:
        if limit is None:
            raise ValueError("--from requires --limit.")
        return f"{base} --from {from_ordinal} --limit {limit}"

    if around_ordinal is not None:
        if context is None:
            raise ValueError("--around requires --context.")
        return f"{base} --around {around_ordinal} --context {context}"

    return base


__all__ = [
    "AbsoluteTranscriptMessage",
    "ParsedSessionTranscript",
    "SessionLogRoute",
    "build_session_log_command",
    "flatten_transcript_segments",
    "parse_session_target",
    "read_session_transcript",
    "route_for_corpus_target",
]
