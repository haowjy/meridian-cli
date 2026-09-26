"""Session search operation with deterministic open commands and corpus scopes."""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path
from shlex import quote
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, computed_field

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import (
    async_from_sync,
    resolve_project_authority,
    resolve_roots_for_read,
    resolve_runtime_authority_for_read,
)
from meridian.lib.ops.session_corpus import SessionCorpusScope, resolve_session_search_corpus
from meridian.lib.ops.session_search_index import SearchProjection
from meridian.lib.ops.session_target import native_source_label
from meridian.lib.ops.session_transcript import (
    AbsoluteTranscriptEntry,
    ParsedSessionTranscript,
    SessionLogRoute,
    build_session_log_command,
    read_session_transcript,
)
from meridian.lib.state.history_index import (
    INITIALIZATION_TIMEOUT,
    QUERY_TIMEOUT,
    HistoryIndex,
    HistoryIndexIncomplete,
)
from meridian.lib.state.native_search_index import SearchRow, discard_native_search_index

_PREVIEW_LIMIT = 200
_OPEN_CONTEXT = 5


class SubsetSearchStep(NamedTuple):
    chat_id: str
    matched: bool
    error: str | None = None


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
    chat_ids: tuple[str, ...] = ()
    source: str | None = None
    segment: int
    segment_start_message: int
    segment_end_message: int
    entry_ordinal: int
    role: str
    content_preview: str
    open_command: str


class SessionSearchOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    matches: tuple[SessionSearchMatch, ...]
    truncated: bool = False
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    sources_total: int = 0
    sources_not_searched: int = 0
    sources_pending: int = 0

    @computed_field
    @property
    def complete(self) -> bool:
        return not self.truncated and not self.errors and not self.sources_not_searched

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        match_label = "match" if len(self.matches) == 1 else "matches"
        if not self.complete:
            headline = f"Session search incomplete — {len(self.matches)} confirmed {match_label}"
        elif self.matches:
            headline = f"Session search — {len(self.matches)} {match_label}"
        else:
            headline = "Session search — no matches"
        searched = max(0, self.sources_total - self.sources_not_searched)
        if self.complete:
            coverage = f"Searched {self.sources_total} sources (complete)."
            if self.warnings:
                coverage = (
                    f"Searched {self.sources_total} sources "
                    f"(complete; {len(self.warnings)} warnings)."
                )
        else:
            reasons: list[str] = []
            if self.sources_pending:
                reasons.append(f"{self.sources_pending} pending")
            unavailable = max(0, self.sources_not_searched - self.sources_pending)
            if unavailable:
                reason_counts = Counter(error.partition(": ")[2] or error for error in self.errors)
                if reason_counts:
                    reasons.extend(
                        f"{count} unavailable ({reason})" for reason, count in reason_counts.items()
                    )
                else:
                    reasons.append(f"{unavailable} unavailable")
            if self.warnings:
                reasons.append(f"{len(self.warnings)} warnings")
            if self.truncated:
                reasons.append("100-hit cap reached")
            if not reasons:
                reasons.append("coverage incomplete")
            coverage = (
                f"Searched {searched}/{self.sources_total} sources (incomplete: "
                f"{'; '.join(reasons)})."
            )
        lines = [headline, coverage]
        for match in self.matches:
            lines.append("")
            lines.append(
                f"--- {match.corpus} :: {match.chat_id} ({match.session_id}) "
                f"entry {match.entry_ordinal} [segment {match.segment}, "
                f"messages {match.segment_start_message}-{match.segment_end_message}] "
                f"[{match.role}] ---"
            )
            if len(match.chat_ids) > 1:
                lines.append("Chats: " + ", ".join(match.chat_ids))
            lines.append(match.content_preview)
            lines.append(f"Open: {match.open_command}")
        return "\n".join(lines)


def _normalize_content(value: str) -> str:
    return " ".join(value.split())


def iter_session_subset_search(
    *, project_root: str, chat_ids: Sequence[str], query: str
) -> Iterator[SubsetSearchStep]:
    """Yield one isolated matched/error step for each requested primary chat."""

    normalized_query = query.strip().lower()
    if not normalized_query:
        raise ValueError("query must not be empty")

    authority = resolve_runtime_authority_for_read(project_root)
    if authority.runtime_root is None:
        for chat_id in chat_ids:
            yield SubsetSearchStep(chat_id, False, f"Chat '{chat_id}' not found")
        return

    try:
        projection = SearchProjection.open(authority.runtime_root, authority.project_root)
        keys = projection.scope(frozenset(chat_ids))
        deadline = time.monotonic() + (INITIALIZATION_TIMEOUT if projection.cold else QUERY_TIMEOUT)
        projection.inspect(keys, deadline=deadline)
        projection.refresh(keys, deadline=deadline)
        matched = {
            chat
            for row in projection.search(
                normalized_query, limit=None, deadline=time.monotonic() + QUERY_TIMEOUT
            )
            for chat in keys[row.key]
        }
    except (ValueError, OSError, sqlite3.Error, TimeoutError) as exc:
        for chat_id in chat_ids:
            yield SubsetSearchStep(chat_id, False, str(exc))
        return
    by_chat = {chat: key for key, chats in keys.items() for chat in chats}
    for chat_id in chat_ids:
        key = by_chat.get(chat_id)
        error = (
            f"unbound: no verified native session for {chat_id}"
            if key is None
            else projection.errors.get(key)
            or (
                "index refreshing — run meridian session index rebuild"
                if key not in projection.fresh
                else None
            )
        )
        yield SubsetSearchStep(chat_id, chat_id in matched, error)


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
    if not transcript.search_ready:
        return matches
    for entry in transcript.all_entries:
        if entry.kind == "setup" and entry.is_placeholder:
            continue
        normalized_content = _normalize_content(entry.content)
        if not normalized_content:
            continue
        if query_lower not in normalized_content.lower():
            continue
        matches.append(
            SessionSearchMatch(
                corpus=corpus,
                chat_id=chat_id,
                session_id=transcript.target.source.session_id,
                source=transcript.target.source.source_label,
                segment=entry.segment_index,
                segment_start_message=entry.start_segment_message_index,
                segment_end_message=entry.end_segment_message_index,
                entry_ordinal=entry.ordinal,
                role=entry.role,
                content_preview=_build_preview(normalized_content, query=query),
                open_command=_build_open_command_for_match(entry=entry, transcript=transcript),
            )
        )
    return matches


def _build_open_command_for_match(
    *,
    entry: AbsoluteTranscriptEntry,
    transcript: ParsedSessionTranscript,
) -> str:
    if entry.kind == "setup":
        return build_session_log_command(
            transcript.route,
            segment_index=entry.segment_index,
            from_ordinal=0,
            limit=1,
        )
    return build_session_log_command(
        transcript.route,
        segment_index=entry.segment_index,
        around_ordinal=entry.ordinal,
        context=_OPEN_CONTEXT,
    )


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
        corpus=transcript.target.source.source_label,
        chat_id=payload.ref.strip() or transcript.target.source.session_id,
    )
    return SessionSearchOutput(
        matches=tuple(matches),
        errors=() if transcript.search_ready else transcript.read_reasons,
        warnings=("; ".join(transcript.read_reasons),)
        if transcript.search_ready and transcript.read_reasons
        else (),
        sources_total=1,
        sources_not_searched=int(not transcript.search_ready),
    )


def _collect_scope(
    payload: SessionSearchInput,
    scope: SessionCorpusScope,
    *,
    cold: bool,
    deadline: float,
    cold_deadline: float,
) -> tuple[SessionCorpusScope, SearchProjection, bool]:
    projection = SearchProjection.open(scope.runtime_root, scope.project_root or scope.runtime_root)
    cold = cold or projection.cold
    if work_id := (payload.work_id or "").strip():
        metadata = HistoryIndex(scope.runtime_root)
        if metadata.classify(deadline=cold_deadline).baseline in {"absent", "outdated"}:
            cold = True
            metadata.initialize(deadline=cold_deadline)
        scope = scope._replace(
            chat_filter=frozenset(
                metadata.work_chat_ids(work_id, deadline=cold_deadline if cold else deadline)
            )
        )
    keys = projection.scope(scope.chat_filter)
    until = cold_deadline if cold else deadline
    projection.inspect(keys, deadline=until)
    projection.refresh(keys, deadline=until)
    return scope, projection, cold


def _render_match(
    row: SearchRow,
    scope: SessionCorpusScope,
    projection: SearchProjection,
    *,
    query: str,
    runtime_root: Path | None,
) -> SessionSearchMatch:
    chats = projection.scope(scope.chat_filter)[row.key]
    command = build_session_log_command(
        SessionLogRoute("ref", chats[0]),
        segment_index=row.segment,
        from_ordinal=0 if row.kind == "setup" else None,
        limit=1 if row.kind == "setup" else None,
        around_ordinal=row.ordinal if row.kind != "setup" else None,
        context=_OPEN_CONTEXT if row.kind != "setup" else None,
    )
    if scope.runtime_root != runtime_root:
        command = (
            "env -u MERIDIAN_PROJECT_DIR -u _MERIDIAN_DEPTH "
            f"_MERIDIAN_RUNTIME_DIR={quote(str(scope.runtime_root))} " + command
        )
    return SessionSearchMatch(
        corpus=scope.label,
        chat_id=chats[0],
        chat_ids=chats,
        session_id=row.key.session_id,
        source=native_source_label(row.key.harness),
        segment=row.segment or 0,
        segment_start_message=row.seg_start or 0,
        segment_end_message=row.seg_end or 0,
        entry_ordinal=row.ordinal,
        role=row.role or "",
        content_preview=_build_preview(row.content, query=query),
        open_command=command,
    )


def _search_corpus(payload: SessionSearchInput, *, query: str) -> SessionSearchOutput:
    roots = resolve_roots_for_read(payload.project_root)
    if roots is None and not (payload.workspace or payload.global_scope):
        return SessionSearchOutput(matches=())
    project_root = (
        roots.project_root
        if roots is not None
        else resolve_project_authority(payload.project_root).project_root
    )
    runtime_root = roots.runtime_root if roots is not None else None
    started = time.monotonic()
    deadline = started + QUERY_TIMEOUT
    cold_deadline = started + INITIALIZATION_TIMEOUT
    try:
        scopes = resolve_session_search_corpus(
            project_root=project_root,
            runtime_root=runtime_root,
            workspace=payload.workspace,
            global_scope=payload.global_scope,
            work_id=payload.work_id,
        )
    except (ValueError, OSError, HistoryIndexIncomplete, sqlite3.Error) as exc:
        return SessionSearchOutput(matches=(), errors=(f"Corpus discovery: {exc}",))

    projections: list[tuple[SessionCorpusScope, SearchProjection]] = []
    candidates: list[tuple[SearchRow, SessionCorpusScope, SearchProjection]] = []
    errors: list[str] = []
    warnings: list[str] = []
    cold = False
    total = not_searched = pending = 0

    def query_scope(scope: SessionCorpusScope, projection: SearchProjection, until: float) -> None:
        if time.monotonic() >= until:
            errors.append(f"{scope.label}: query deadline exceeded")
            return
        try:
            candidates.extend(
                (row, scope, projection) for row in projection.search(query, deadline=until)
            )
        except (sqlite3.DatabaseError, TimeoutError) as exc:
            if isinstance(exc, sqlite3.DatabaseError) and not any(
                reason in str(exc) for reason in ("locked", "interrupted")
            ):
                discard_native_search_index(scope.runtime_root)
            errors.append(f"{scope.label}: {exc}")

    for position, scope in enumerate(scopes):
        if time.monotonic() >= (cold_deadline if cold else deadline):
            errors.append(
                f"{len(scopes) - position} runtime roots not searched (deadline exceeded)"
            )
            break
        try:
            scope, projection, cold = _collect_scope(
                payload, scope, cold=cold, deadline=deadline, cold_deadline=cold_deadline
            )
            keys = projection.scope(scope.chat_filter)
            total += len(keys)
            not_searched += len(keys) - len(projection.fresh)
            pending += len(keys.keys() - projection.fresh - projection.errors.keys())
            errors.extend(
                f"{scope.label} {', '.join(keys[key])}: {error}"
                for key, error in projection.errors.items()
            )
            warnings.extend(
                f"{scope.label} {', '.join(keys[key])}: {warning}"
                for key, warning in projection.warnings.items()
            )
            if cold:
                projections.append((scope, projection))
            else:
                query_scope(scope, projection, deadline)
        except (ValueError, OSError, HistoryIndexIncomplete, sqlite3.Error) as exc:
            errors.append(f"{scope.label}: {exc}")
    if cold:
        deadline = time.monotonic() + QUERY_TIMEOUT
        for scope, projection in projections:
            query_scope(scope, projection, deadline)
    candidates.sort(key=lambda item: (-item[0].activity, item[0].segment or 0, item[0].ordinal))
    matches = tuple(
        _render_match(row, scope, projection, query=query, runtime_root=runtime_root)
        for row, scope, projection in candidates[:100]
    )
    return SessionSearchOutput(
        matches=tuple(matches),
        truncated=len(candidates) > 100,
        errors=tuple(errors),
        warnings=tuple(warnings),
        sources_total=total,
        sources_not_searched=not_searched,
        sources_pending=pending,
    )


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
    "SubsetSearchStep",
    "iter_session_subset_search",
    "session_search",
    "session_search_sync",
]
