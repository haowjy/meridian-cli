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
    source_control_root: Path | None = None
    target_control_root: Path | None = None
    source_config_root: Path | None = None
    target_config_root: Path | None = None


def resolve_claude_session_access_source(
    session: SessionRequest,
    *,
    control_root: Path,
    materialization_root: Path | None,
    target_config_root: Path | None,
) -> ClaudeSessionAccessSource:
    """Resolve Claude transcript-seeding inputs from one session request.

    Explicit source control-root metadata wins. Legacy/partial requests fall
    back to the current launch control root so task cwd is never treated as
    Claude project authority.
    """

    source_session_id = (session.requested_harness_session_id or "").strip() or None
    if source_session_id is None:
        return ClaudeSessionAccessSource(should_seed=False)

    source_control_root = (session.source_control_root or "").strip() or None
    source_config_root = (session.source_claude_config_dir or "").strip() or None

    resolved_source_control_root = (
        Path(source_control_root) if source_control_root else control_root
    )

    return ClaudeSessionAccessSource(
        should_seed=True,
        source_session_id=source_session_id,
        source_control_root=resolved_source_control_root,
        target_control_root=control_root,
        source_config_root=(
            Path(source_config_root) if source_config_root else materialization_root
        ),
        target_config_root=target_config_root,
    )


__all__ = ["ClaudeSessionAccessSource", "resolve_claude_session_access_source"]
