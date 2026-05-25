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


class AbsoluteTranscriptEntry(NamedTuple):
    ordinal: int
    segment_index: int
    start_segment_message_index: int
    end_segment_message_index: int
    role: str
    content: str
    messages: tuple[AbsoluteTranscriptMessage, ...]


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
    entries: tuple[AbsoluteTranscriptEntry, ...]


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


def _is_tool_result_message(message: AbsoluteTranscriptMessage) -> bool:
    return message.role == "user" and message.content.startswith("[tool_result]")


def _is_plain_user_message(message: AbsoluteTranscriptMessage) -> bool:
    return message.role == "user" and not _is_tool_result_message(message)


def group_transcript_entries(
    messages: tuple[AbsoluteTranscriptMessage, ...],
) -> tuple[AbsoluteTranscriptEntry, ...]:
    if not messages:
        return ()

    user_leads_to_tool_result = [False] * len(messages)
    seen_tool_result = False
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if _is_tool_result_message(message):
            seen_tool_result = True
            continue
        if _is_plain_user_message(message):
            user_leads_to_tool_result[index] = seen_tool_result
            seen_tool_result = False

    chunks: list[list[AbsoluteTranscriptMessage]] = []
    current: list[AbsoluteTranscriptMessage] = []

    for index, message in enumerate(messages):
        if current and (
            message.segment_index != current[-1].segment_index or _is_plain_user_message(message)
        ):
            chunks.append(current)
            current = []

        current.append(message)

        next_message = messages[index + 1] if index + 1 < len(messages) else None
        should_close = False
        if _is_tool_result_message(message):
            should_close = next_message is None or not _is_tool_result_message(next_message)
        elif _is_plain_user_message(message):
            should_close = not user_leads_to_tool_result[index]
        elif (
            next_message is None
            or message.segment_index != next_message.segment_index
            or _is_plain_user_message(next_message)
        ):
            should_close = True

        if should_close:
            chunks.append(current)
            current = []

    if current:
        chunks.append(current)

    entries: list[AbsoluteTranscriptEntry] = []
    for ordinal, chunk in enumerate(chunks, start=1):
        first = chunk[0]
        last = chunk[-1]
        role = first.role if all(message.role == first.role for message in chunk) else "mixed"
        entries.append(
            AbsoluteTranscriptEntry(
                ordinal=ordinal,
                segment_index=first.segment_index,
                start_segment_message_index=first.segment_message_index,
                end_segment_message_index=last.segment_message_index,
                role=role,
                content="\n\n".join(message.content for message in chunk),
                messages=tuple(chunk),
            )
        )

    return tuple(entries)


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
    flattened = flatten_transcript_segments(segments)
    return ParsedSessionTranscript(
        project_root=project_root,
        runtime_root=runtime_root,
        target=target,
        route=route,
        segments=segments,
        total_compactions=total_compactions,
        messages=flattened,
        entries=group_transcript_entries(flattened),
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
    "AbsoluteTranscriptEntry",
    "AbsoluteTranscriptMessage",
    "ParsedSessionTranscript",
    "SessionLogRoute",
    "build_session_log_command",
    "flatten_transcript_segments",
    "group_transcript_entries",
    "parse_session_target",
    "read_session_transcript",
    "route_for_corpus_target",
]
