"""Session search operation with deterministic open commands and corpus scopes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.ops.session_corpus import resolve_session_search_corpus
from meridian.lib.ops.session_target import resolve_session_log_target
from meridian.lib.ops.session_transcript import (
    ParsedSessionTranscript,
    build_session_log_command,
    parse_session_target,
    read_session_transcript,
    route_for_corpus_target,
)
from meridian.lib.state import session_store

_PREVIEW_LIMIT = 200
_OPEN_CONTEXT = 5


class SessionSearchInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = ""
    ref: str = ""
    file_path: str | None = None
    project_root: str | None = None
    work_id: str | None = None
    workspace: bool = False
    global_scope: bool = False


class SessionSearchMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    corpus: str
    chat_id: str
    session_id: str
    source: str | None = None
    segment: int
    message_index: int
    message_ordinal: int
    role: str
    content_preview: str
    open_command: str


class SessionSearchOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    matches: tuple[SessionSearchMatch, ...]

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not self.matches:
            return "Session search — no matches"

        match_label = "match" if len(self.matches) == 1 else "matches"
        lines = [f"Session search — {len(self.matches)} {match_label}"]
        for match in self.matches:
            lines.append("")
            lines.append(
                f"--- {match.corpus} :: {match.chat_id} ({match.session_id}) "
                f"message {match.message_ordinal} [segment {match.segment}, "
                f"message {match.message_index}] [{match.role}] ---"
            )
            lines.append(match.content_preview)
            lines.append(f"Open: {match.open_command}")
        return "\n".join(lines)


def _normalize_content(value: str) -> str:
    return " ".join(value.split())


def _build_preview(content: str, *, query: str, limit: int = _PREVIEW_LIMIT) -> str:
    if not content:
        return ""

    normalized_query = query.lower()
    lowered = content.lower()
    match_start = lowered.find(normalized_query)
    if match_start < 0:
        return content if len(content) <= limit else f"{content[: limit - 3].rstrip()}..."

    if len(content) <= limit:
        window_start = 0
        window_end = len(content)
    else:
        half_context = max((limit - len(query)) // 2, 0)
        window_start = max(match_start - half_context, 0)
        window_end = min(window_start + limit, len(content))
        window_start = max(window_end - limit, 0)

    snippet = content[window_start:window_end]
    local_start = match_start - window_start
    local_end = local_start + len(query)
    highlighted = (
        f"{snippet[:local_start]}[[{snippet[local_start:local_end]}]]{snippet[local_end:]}"
    )

    prefix = "..." if window_start > 0 else ""
    suffix = "..." if window_end < len(content) else ""
    return f"{prefix}{highlighted}{suffix}"


def _matches_for_transcript(
    *,
    transcript: ParsedSessionTranscript,
    query: str,
    query_lower: str,
    corpus: str,
    chat_id: str,
) -> list[SessionSearchMatch]:
    matches: list[SessionSearchMatch] = []
    for message in transcript.messages:
        normalized_content = _normalize_content(message.content)
        if not normalized_content:
            continue
        if query_lower not in normalized_content.lower():
            continue
        matches.append(
            SessionSearchMatch(
                corpus=corpus,
                chat_id=chat_id,
                session_id=transcript.target.session_id,
                source=transcript.target.source,
                segment=message.segment_index,
                message_index=message.segment_message_index,
                message_ordinal=message.ordinal,
                role=message.role,
                content_preview=_build_preview(normalized_content, query=query),
                open_command=build_session_log_command(
                    transcript.route,
                    around_ordinal=message.ordinal,
                    context=_OPEN_CONTEXT,
                ),
            )
        )
    return matches


def _search_single_target(payload: SessionSearchInput, *, query: str) -> SessionSearchOutput:
    transcript = read_session_transcript(
        ref=payload.ref,
        file_path=payload.file_path,
        project_root=payload.project_root,
    )
    query_lower = query.lower()
    matches = _matches_for_transcript(
        transcript=transcript,
        query=query,
        query_lower=query_lower,
        corpus=transcript.target.source or "session",
        chat_id=payload.ref.strip() or transcript.target.session_id,
    )
    return SessionSearchOutput(matches=tuple(matches))


def _search_corpus(payload: SessionSearchInput, *, query: str) -> SessionSearchOutput:
    roots = resolve_roots_for_read(payload.project_root)
    scopes = resolve_session_search_corpus(
        project_root=roots.project_root,
        runtime_root=roots.runtime_root,
        workspace=payload.workspace,
        global_scope=payload.global_scope,
        work_id=payload.work_id,
    )

    matches: list[SessionSearchMatch] = []
    query_lower = query.lower()
    for scope in scopes:
        records = session_store.list_all_session_records(scope.runtime_root)
        for record in records:
            if scope.chat_filter is not None and record.chat_id not in scope.chat_filter:
                continue

            project_root = scope.project_root or scope.runtime_root
            try:
                target = resolve_session_log_target(
                    ref=record.chat_id,
                    file_path=None,
                    project_root=project_root,
                    runtime_root=scope.runtime_root,
                )
            except (ValueError, FileNotFoundError, OSError):
                continue

            transcript = parse_session_target(
                project_root=project_root,
                runtime_root=scope.runtime_root,
                target=target,
                route=route_for_corpus_target(target),
            )
            matches.extend(
                _matches_for_transcript(
                    transcript=transcript,
                    query=query,
                    query_lower=query_lower,
                    corpus=scope.label,
                    chat_id=record.chat_id,
                )
            )

    matches.sort(key=lambda match: (match.corpus, match.chat_id, match.message_ordinal))
    return SessionSearchOutput(matches=tuple(matches))


def session_search_sync(
    payload: SessionSearchInput,
    ctx: RuntimeContext | None = None,
) -> SessionSearchOutput:
    _ = ctx
    query = payload.query.strip()
    if not query:
        raise ValueError("query must not be empty")
    if payload.file_path and (payload.workspace or payload.global_scope or payload.work_id):
        raise ValueError("--file cannot be combined with search scope flags.")

    if payload.ref.strip() or (payload.file_path and payload.file_path.strip()):
        if payload.workspace or payload.global_scope or payload.work_id:
            raise ValueError("REF/--file cannot be combined with --workspace/--global/--work.")
        return _search_single_target(payload, query=query)

    return _search_corpus(payload, query=query)


session_search = async_from_sync(session_search_sync)


__all__ = [
    "SessionSearchInput",
    "SessionSearchMatch",
    "SessionSearchOutput",
    "session_search",
    "session_search_sync",
]
