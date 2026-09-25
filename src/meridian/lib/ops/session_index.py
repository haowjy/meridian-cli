"""Explicit finite history index inspection and rebuild operations."""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.settings import load_config
from meridian.lib.harness.transcript_preview import TRANSCRIPT_PREVIEW_VERSION
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.ops.session_search_index import SearchProjection, native_bindings
from meridian.lib.state.history_changes import HistoryChanges
from meridian.lib.state.history_index import QUERY_TIMEOUT, HistoryIndex
from meridian.lib.state.native_search_index import INDEX_FILENAME, PARSER_VERSION, witness_json


class SessionIndexInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    project_root: str | None = None
    action: Literal["status", "rebuild"] = "status"
    reset: bool = False
    metadata_only: bool = False


class SessionIndexOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    baseline: str
    schema_version: int | None = None
    coverage: dict[str, object] | None = None
    reason: str | None = None
    pending_sources: int = 0
    preview_cached: int = 0
    preview_unavailable: int | None = None
    search_fresh: int = 0
    search_stale: int = 0
    search_unindexed: int = 0
    search_bytes: int = 0
    search_unavailable: int = 0

    def format_text(self, ctx: object = None) -> str:
        text = (
            f"History index: {self.baseline}; pending sources: {self.pending_sources}; "
            f"cached previews: {self.preview_cached}"
        )
        if self.preview_unavailable is not None:
            text += f"; unavailable in warm pass: {self.preview_unavailable}"
        text += (
            f"\nNative search: {self.search_fresh} fresh, {self.search_stale} stale, "
            f"{self.search_unindexed} unindexed; {self.search_unavailable} unavailable; "
            f"{self.search_bytes} bytes"
        )
        if self.reason:
            text += f"\n{self.reason}"
        return text


def session_index_sync(payload: SessionIndexInput) -> SessionIndexOutput:
    roots = resolve_roots_for_read(payload.project_root)
    if roots is None:
        return SessionIndexOutput(baseline="absent")
    index = HistoryIndex(roots.runtime_root)
    if payload.action == "status":
        deadline = time.monotonic() + QUERY_TIMEOUT
        status = index.inspect(deadline=deadline)
        _, pending = HistoryChanges(roots.runtime_root).inspect(
            timeout=max(0.0, deadline - time.monotonic())
        )
        search = _search_status(roots.runtime_root, roots.project_root, deadline=deadline)
        return SessionIndexOutput(
            **search,
            baseline=status.baseline,
            schema_version=status.schema,
            reason=status.reason,
            pending_sources=len(pending),
            preview_cached=(
                index.preview_count(preview_version=TRANSCRIPT_PREVIEW_VERSION, deadline=deadline)
                if status.baseline == "current"
                else 0
            ),
        )
    if payload.action == "rebuild":
        from meridian.lib.state.retention_archive import import_archive

        destination = load_config(roots.project_root).history.archive.destination
        if destination:
            directory = Path(destination).expanduser()
            if directory.is_dir():
                for archive in sorted(directory.glob("meridian-history-*.zip")):
                    import_archive(roots.runtime_root, archive, select=False)
    coverage = index.rebuild(reset=payload.reset)
    search: SearchStatus = {}
    if not payload.metadata_only:
        projection = SearchProjection.open(roots.runtime_root, roots.project_root)
        if projection.index:
            projection.index.rebuild()
        projection.stored.clear()
        projection.inspect(projection.bindings, deadline=float("inf"))
        projection.refresh(projection.bindings, deadline=float("inf"), rebuild=True)
        search = _search_status(roots.runtime_root, roots.project_root, deadline=float("inf"))
    _, pending = HistoryChanges(roots.runtime_root).inspect()
    return SessionIndexOutput(
        baseline="complete" if coverage.complete else "incomplete",
        coverage=asdict(coverage),
        pending_sources=len(pending),
        preview_cached=index.preview_count(preview_version=TRANSCRIPT_PREVIEW_VERSION),
        **search,
    )


class SearchStatus(TypedDict, total=False):
    search_fresh: int
    search_stale: int
    search_unindexed: int
    search_bytes: int
    search_unavailable: int


def _search_status(runtime_root: Path, project_root: Path, *, deadline: float) -> SearchStatus:
    if not (runtime_root / "history-index" / INDEX_FILENAME).exists():
        return SearchStatus(search_unindexed=len(native_bindings(runtime_root)))
    projection = SearchProjection.open(runtime_root, project_root)
    projection.inspect(projection.bindings, deadline=deadline)
    indexed = projection.stored.keys() & projection.bindings.keys()
    current = {
        key
        for key in indexed
        if key in projection.sources
        and (projection.stored[key].witness, projection.stored[key].parser_version)
        == (witness_json(projection.sources[key].witness), PARSER_VERSION)
    }
    return SearchStatus(
        search_fresh=len(current),
        search_stale=len(indexed - current),
        search_unavailable=len(projection.errors),
        search_unindexed=len(projection.bindings.keys() - indexed),
        search_bytes=projection.index.path.stat().st_size if projection.index else 0,
    )


session_index = async_from_sync(session_index_sync)
