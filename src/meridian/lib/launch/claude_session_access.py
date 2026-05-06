"""Shared Claude continue/fork session-access resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meridian.lib.launch.request import SessionRequest


@dataclass(frozen=True)
class ClaudeSessionAccessSource:
    """Resolved source/target data for Claude transcript seeding."""

    should_seed: bool
    source_session_id: str | None = None
    source_cwd: Path | None = None
    source_config_root: Path | None = None
    target_config_root: Path | None = None


def resolve_claude_session_access_source(
    session: SessionRequest,
    *,
    child_cwd: Path,
    materialization_root: Path | None,
    target_config_root: Path | None,
) -> ClaudeSessionAccessSource:
    """Resolve Claude transcript-seeding inputs from one session request.

    Explicit tracked source metadata wins. When a raw/untracked ref lacks
    tracked source metadata, fall back to the child's cwd plus the durable
    materialization root so continue/fork can still access the session.
    """

    source_session_id = (session.requested_harness_session_id or "").strip() or None
    if source_session_id is None:
        return ClaudeSessionAccessSource(should_seed=False)

    source_cwd = (session.source_execution_cwd or "").strip() or None
    source_config_root = (session.source_claude_config_dir or "").strip() or None

    if source_cwd is not None:
        return ClaudeSessionAccessSource(
            should_seed=True,
            source_session_id=source_session_id,
            source_cwd=Path(source_cwd),
            source_config_root=Path(source_config_root) if source_config_root else None,
            target_config_root=target_config_root,
        )

    if session.continue_source_tracked:
        return ClaudeSessionAccessSource(should_seed=False)

    return ClaudeSessionAccessSource(
        should_seed=True,
        source_session_id=source_session_id,
        source_cwd=child_cwd,
        source_config_root=materialization_root,
        target_config_root=target_config_root,
    )


__all__ = ["ClaudeSessionAccessSource", "resolve_claude_session_access_source"]
