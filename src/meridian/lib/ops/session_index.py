"""Explicit finite history index inspection and rebuild operations."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.settings import load_config
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.state.history_changes import HistoryChanges
from meridian.lib.state.history_index import HistoryIndex


class SessionIndexInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    project_root: str | None = None
    action: Literal["status", "rebuild"] = "status"
    reset: bool = False
    metadata_only: bool = False


class SessionIndexOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    baseline: str
    coverage: dict[str, object] | None = None
    pending_sources: int = 0
    preview_cached: int = 0
    preview_unavailable: int | None = None

    def format_text(self, ctx: object = None) -> str:
        text = (
            f"History index: {self.baseline}; pending sources: {self.pending_sources}; "
            f"cached previews: {self.preview_cached}"
        )
        if self.preview_unavailable is not None:
            text += f"; unavailable in warm pass: {self.preview_unavailable}"
        return text


def session_index_sync(payload: SessionIndexInput) -> SessionIndexOutput:
    roots = resolve_roots_for_read(payload.project_root)
    if roots is None:
        return SessionIndexOutput(baseline="absent")
    index = HistoryIndex(roots.runtime_root)
    if payload.action == "status" and not index.path.exists():
        return SessionIndexOutput(baseline="absent")
    if payload.action == "rebuild":
        from meridian.lib.state.retention_archive import import_archive

        destination = load_config(roots.project_root).history.archive.destination
        if destination:
            directory = Path(destination).expanduser()
            if directory.is_dir():
                for archive in sorted(directory.glob("meridian-history-*.zip")):
                    import_archive(roots.runtime_root, archive, select=False)
    coverage = (
        index.rebuild(reset=payload.reset) if payload.action == "rebuild" else index.catch_up()
    )
    unavailable: int | None = None
    if payload.action == "rebuild" and not payload.metadata_only:
        from meridian.lib.ops.session_preview import PreviewIdentity, SessionPreview

        unavailable = 0
        reader = SessionPreview(str(roots.project_root))
        for ref, history_id, generation in index.preview_references():
            identity = PreviewIdentity(ref, history_id, generation)
            reader.refresh(identity, lambda: True)
            if reader.peek(identity) is None:
                unavailable += 1
    _, pending = HistoryChanges(roots.runtime_root).capture()
    return SessionIndexOutput(
        baseline="complete" if coverage.complete else "incomplete",
        coverage=asdict(coverage),
        pending_sources=len(pending),
        preview_cached=index.preview_count(),
        preview_unavailable=unavailable,
    )


session_index = async_from_sync(session_index_sync)
