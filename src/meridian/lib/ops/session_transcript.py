"""Canonical session transcript read + flatten boundary for session surfaces."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Literal, NamedTuple

from meridian.lib.harness.transcript import TranscriptMessage, parse_transcript_file_with_prologues
from meridian.lib.ops.runtime import resolve_runtime_authority_for_read
from meridian.lib.ops.session_target import SessionLogTarget, resolve_session_log_target

_PROLOGUE_PLACEHOLDER = "[prologue slot reserved: no extractable system prompt]"
_HANDOFF_PLACEHOLDER = "[compaction handoff slot reserved: no extractable handoff]"


class AbsoluteTranscriptMessage(NamedTuple):
    ordinal: int
    segment_index: int
    segment_message_index: int
    role: str
    content: str


class AbsoluteTranscriptEntry(NamedTuple):
    ordinal: int
    absolute_ordinal: int | None
    segment_index: int
    start_segment_message_index: int
    end_segment_message_index: int
    role: str
    content: str
    messages: tuple[AbsoluteTranscriptMessage, ...]
    kind: Literal["prologue", "interaction"]


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
    segment_prologues: tuple[str | None, ...]
    messages: tuple[AbsoluteTranscriptMessage, ...]
    entries: tuple[AbsoluteTranscriptEntry, ...]
    segment_entries: tuple[tuple[AbsoluteTranscriptEntry, ...], ...]


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


def _is_interaction_message(message: AbsoluteTranscriptMessage) -> bool:
    return message.role in {"assistant", "user"}


def group_transcript_entries(
    messages: tuple[AbsoluteTranscriptMessage, ...],
) -> tuple[AbsoluteTranscriptEntry, ...]:
    interaction_messages = tuple(
        message for message in messages if _is_interaction_message(message)
    )
    if not interaction_messages:
        return ()

    user_leads_to_tool_result = [False] * len(interaction_messages)
    seen_tool_result = False
    for index in range(len(interaction_messages) - 1, -1, -1):
        message = interaction_messages[index]
        if _is_tool_result_message(message):
            seen_tool_result = True
            continue
        if _is_plain_user_message(message):
            user_leads_to_tool_result[index] = seen_tool_result
            seen_tool_result = False

    chunks: list[list[AbsoluteTranscriptMessage]] = []
    current: list[AbsoluteTranscriptMessage] = []

    for index, message in enumerate(interaction_messages):
        if current and (
            message.segment_index != current[-1].segment_index or _is_plain_user_message(message)
        ):
            chunks.append(current)
            current = []

        current.append(message)

        next_message = (
            interaction_messages[index + 1] if index + 1 < len(interaction_messages) else None
        )
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
    local_ordinals: dict[int, int] = {}
    absolute_ordinal = 1
    for chunk in chunks:
        first = chunk[0]
        last = chunk[-1]
        role = first.role if all(message.role == first.role for message in chunk) else "mixed"
        segment_local_ordinal = local_ordinals.get(first.segment_index, 1)
        local_ordinals[first.segment_index] = segment_local_ordinal + 1
        entries.append(
            AbsoluteTranscriptEntry(
                ordinal=segment_local_ordinal,
                absolute_ordinal=absolute_ordinal,
                segment_index=first.segment_index,
                start_segment_message_index=first.segment_message_index,
                end_segment_message_index=last.segment_message_index,
                role=role,
                content="\n\n".join(message.content for message in chunk),
                messages=tuple(chunk),
                kind="interaction",
            )
        )
        absolute_ordinal += 1

    return tuple(entries)


def _prologue_content_for_segment(
    *,
    segment_index: int,
    segment_prologues: tuple[str | None, ...],
) -> str:
    if segment_index < len(segment_prologues):
        extracted = (segment_prologues[segment_index] or "").strip()
        if extracted:
            return extracted
    return _PROLOGUE_PLACEHOLDER if segment_index == 0 else _HANDOFF_PLACEHOLDER


def build_segment_entries(
    *,
    segments: list[list[TranscriptMessage]],
    segment_prologues: tuple[str | None, ...],
    interaction_entries: tuple[AbsoluteTranscriptEntry, ...],
) -> tuple[tuple[AbsoluteTranscriptEntry, ...], ...]:
    entries_by_segment: dict[int, list[AbsoluteTranscriptEntry]] = {}
    for entry in interaction_entries:
        entries_by_segment.setdefault(entry.segment_index, []).append(entry)

    segment_entries: list[tuple[AbsoluteTranscriptEntry, ...]] = []
    for segment_index, _segment_messages in enumerate(segments):
        prologue = AbsoluteTranscriptEntry(
            ordinal=0,
            absolute_ordinal=None,
            segment_index=segment_index,
            start_segment_message_index=0,
            end_segment_message_index=0,
            role="system",
            content=_prologue_content_for_segment(
                segment_index=segment_index,
                segment_prologues=segment_prologues,
            ),
            messages=(),
            kind="prologue",
        )
        interaction = tuple(entries_by_segment.get(segment_index, []))
        segment_entries.append((prologue, *interaction))

    return tuple(segment_entries)


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
    parsed = parse_transcript_file_with_prologues(target.file_path)
    flattened = flatten_transcript_segments(parsed.segments)
    interaction_entries = group_transcript_entries(flattened)
    return ParsedSessionTranscript(
        project_root=project_root,
        runtime_root=runtime_root,
        target=target,
        route=route,
        segments=parsed.segments,
        total_compactions=parsed.total_compactions,
        segment_prologues=parsed.segment_prologues,
        messages=flattened,
        entries=interaction_entries,
        segment_entries=build_segment_entries(
            segments=parsed.segments,
            segment_prologues=parsed.segment_prologues,
            interaction_entries=interaction_entries,
        ),
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
    segment_index: int | None = None,
    global_scope: bool = False,
    from_ordinal: int | None = None,
    before_ordinal: int | None = None,
    limit: int | None = None,
    around_ordinal: int | None = None,
    context: int | None = None,
) -> str:
    if route.mode == "file":
        base = f"meridian session log --file {shlex.quote(route.value)}"
    else:
        base = f"meridian session log {shlex.quote(route.value)}"
    if global_scope:
        if segment_index is not None:
            raise ValueError("--global cannot be combined with --segment.")
        base = f"{base} --global"
    if segment_index is not None:
        base = f"{base} --segment {segment_index}"

    if from_ordinal is not None:
        if limit is None:
            raise ValueError("--from requires --limit.")
        return f"{base} --from {from_ordinal} --limit {limit}"

    if before_ordinal is not None:
        if limit is None:
            raise ValueError("--before requires --limit.")
        return f"{base} --before {before_ordinal} --limit {limit}"

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
    "build_segment_entries",
    "build_session_log_command",
    "flatten_transcript_segments",
    "group_transcript_entries",
    "parse_session_target",
    "read_session_transcript",
    "route_for_corpus_target",
]
