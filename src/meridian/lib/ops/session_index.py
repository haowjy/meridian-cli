"""Explicit finite history index inspection and rebuild operations."""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.settings import load_config
from meridian.lib.harness.transcript_preview import TRANSCRIPT_PREVIEW_VERSION
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.ops.session_search_index import SearchProjection, SearchStatus
from meridian.lib.state.history_changes import HistoryChanges
from meridian.lib.state.history_index import QUERY_TIMEOUT, HistoryIndex


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
    search_fresh: int | None = None
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
        if self.search_fresh is not None:
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
        search = SearchProjection.read_status(
            roots.runtime_root, roots.project_root, deadline=deadline
        )
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
        search = projection.rebuild()
    _, pending = HistoryChanges(roots.runtime_root).inspect()
    return SessionIndexOutput(
        baseline="complete" if coverage.complete else "incomplete",
        coverage=asdict(coverage),
        pending_sources=len(pending),
        preview_cached=index.preview_count(preview_version=TRANSCRIPT_PREVIEW_VERSION),
        **search,
    )


session_index = async_from_sync(session_index_sync)
