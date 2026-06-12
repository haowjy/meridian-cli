"""Canonical session transcript read + flatten boundary for session surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, NamedTuple

from meridian.lib.core.command_strings import format_command_for_display
from meridian.lib.harness.transcript import (
    ToolCall,
    TranscriptMessage,
    TranscriptParseResult,
    parse_opencode_db_transcript_with_prologues,
    parse_transcript_file_with_prologues,
)
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
    tool_call: ToolCall | None = None
    is_tool_result: bool = False


class AbsoluteTranscriptEntry(NamedTuple):
    ordinal: int
    global_ordinal: int
    segment_index: int
    start_segment_message_index: int
    end_segment_message_index: int
    role: str
    content: str
    messages: tuple[AbsoluteTranscriptMessage, ...]
    kind: Literal["setup", "interaction"]
    is_placeholder: bool = False


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
    segment_setups: tuple[str | None, ...]
    messages: tuple[AbsoluteTranscriptMessage, ...]
    entries: tuple[AbsoluteTranscriptEntry, ...]
    all_entries: tuple[AbsoluteTranscriptEntry, ...]
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
                    tool_call=message.tool_call,
                    is_tool_result=message.is_tool_result,
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
    for chunk in chunks:
        first = chunk[0]
        last = chunk[-1]
        role = first.role if all(message.role == first.role for message in chunk) else "mixed"
        segment_local_ordinal = local_ordinals.get(first.segment_index, 1)
        local_ordinals[first.segment_index] = segment_local_ordinal + 1
        entries.append(
            AbsoluteTranscriptEntry(
                ordinal=segment_local_ordinal,
                global_ordinal=-1,
                segment_index=first.segment_index,
                start_segment_message_index=first.segment_message_index,
                end_segment_message_index=last.segment_message_index,
                role=role,
                content="\n\n".join(message.content for message in chunk),
                messages=tuple(chunk),
                kind="interaction",
            )
        )

    return tuple(entries)


def _setup_content_for_segment(
    *,
    segment_index: int,
    segment_setups: tuple[str | None, ...],
) -> tuple[str, bool]:
    if segment_index < len(segment_setups):
        extracted = (segment_setups[segment_index] or "").strip()
        if extracted:
            return (extracted, False)
    return (
        (_PROLOGUE_PLACEHOLDER if segment_index == 0 else _HANDOFF_PLACEHOLDER),
        True,
    )


def build_segment_entries(
    *,
    segments: list[list[TranscriptMessage]],
    segment_setups: tuple[str | None, ...],
    interaction_entries: tuple[AbsoluteTranscriptEntry, ...],
) -> tuple[tuple[AbsoluteTranscriptEntry, ...], ...]:
    entries_by_segment: dict[int, list[AbsoluteTranscriptEntry]] = {}
    for entry in interaction_entries:
        entries_by_segment.setdefault(entry.segment_index, []).append(entry)

    segment_entries: list[tuple[AbsoluteTranscriptEntry, ...]] = []
    global_ordinal = 0
    for segment_index, _segment_messages in enumerate(segments):
        setup_content, is_placeholder = _setup_content_for_segment(
            segment_index=segment_index,
            segment_setups=segment_setups,
        )
        setup_entry = AbsoluteTranscriptEntry(
            ordinal=0,
            global_ordinal=global_ordinal,
            segment_index=segment_index,
            start_segment_message_index=0,
            end_segment_message_index=0,
            role="system",
            content=setup_content,
            messages=(),
            kind="setup",
            is_placeholder=is_placeholder,
        )
        global_ordinal += 1

        interaction_entries_for_segment: list[AbsoluteTranscriptEntry] = []
        for interaction in entries_by_segment.get(segment_index, []):
            interaction_entries_for_segment.append(
                AbsoluteTranscriptEntry(
                    ordinal=interaction.ordinal,
                    global_ordinal=global_ordinal,
                    segment_index=interaction.segment_index,
                    start_segment_message_index=interaction.start_segment_message_index,
                    end_segment_message_index=interaction.end_segment_message_index,
                    role=interaction.role,
                    content=interaction.content,
                    messages=interaction.messages,
                    kind=interaction.kind,
                    is_placeholder=False,
                )
            )
            global_ordinal += 1
        segment_entries.append((setup_entry, *tuple(interaction_entries_for_segment)))

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
    if target.file_path is None:
        return SessionLogRoute(mode="ref", value=target.session_id)
    return SessionLogRoute(mode="file", value=str(target.file_path))


def _parse_target_source(target: SessionLogTarget) -> TranscriptParseResult:
    if target.transcript_source == "opencode_db":
        return parse_opencode_db_transcript_with_prologues(target.session_id)
    if target.file_path is None:
        raise FileNotFoundError(f"Session file for '{target.session_id}' not found")
    return parse_transcript_file_with_prologues(target.file_path)


def _has_usable_interaction_content(parsed: TranscriptParseResult) -> bool:
    return any(
        message.role in {"assistant", "user"} and message.content.strip()
        for segment in parsed.segments
        for message in segment
    )


def parse_session_target(
    *,
    project_root: Path,
    runtime_root: Path,
    target: SessionLogTarget,
    route: SessionLogRoute,
) -> ParsedSessionTranscript:
    parsed = _parse_target_source(target)
    if target.transcript_source == "opencode_db" and not _has_usable_interaction_content(parsed):
        for fallback_target in target.fallback_targets:
            fallback = parse_session_target(
                project_root=project_root,
                runtime_root=runtime_root,
                target=fallback_target,
                route=route,
            )
            if fallback.entries:
                return fallback
    flattened = flatten_transcript_segments(parsed.segments)
    interaction_entries = group_transcript_entries(flattened)
    segment_entries = build_segment_entries(
        segments=parsed.segments,
        segment_setups=parsed.segment_setups,
        interaction_entries=interaction_entries,
    )
    all_entries = tuple(entry for segment in segment_entries for entry in segment)
    resolved_interaction_entries = tuple(
        entry for entry in all_entries if entry.kind == "interaction"
    )
    return ParsedSessionTranscript(
        project_root=project_root,
        runtime_root=runtime_root,
        target=target,
        route=route,
        segments=parsed.segments,
        total_compactions=parsed.total_compactions,
        segment_setups=parsed.segment_setups,
        messages=flattened,
        entries=resolved_interaction_entries,
        all_entries=all_entries,
        segment_entries=segment_entries,
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
    command: list[str] = ["meridian", "session", "log"]
    if route.mode == "file":
        command.extend(["--file", route.value])
    else:
        command.append(route.value)
    if global_scope:
        if segment_index is not None:
            raise ValueError("--global cannot be combined with --segment.")
        command.append("--global")
    if segment_index is not None:
        command.extend(["--segment", str(segment_index)])

    if from_ordinal is not None:
        if limit is None:
            raise ValueError("--from requires --limit.")
        command.extend(["--from", str(from_ordinal), "--limit", str(limit)])
        return format_command_for_display(command)

    if before_ordinal is not None:
        if limit is None:
            raise ValueError("--before requires --limit.")
        command.extend(["--before", str(before_ordinal), "--limit", str(limit)])
        return format_command_for_display(command)

    if around_ordinal is not None:
        if context is None:
            raise ValueError("--around requires --context.")
        command.extend(["--around", str(around_ordinal), "--context", str(context)])
        return format_command_for_display(command)

    return format_command_for_display(command)


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
