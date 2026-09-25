"""Session-reference repair target resolution."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from pathlib import Path
from typing import NamedTuple

from meridian.lib.ops.session_target import (
    NativeSessionUnavailable,
    _is_chat_ref,
    _is_spawn_ref,
    _resolve_from_chat_id,
    _resolve_from_spawn_id,
    _untracked_target,
)


class SessionRepairTarget(NamedTuple):
    detected_harness_session_id: str | None
    source: str | None
    reason: str | None = None


def _resolve_repair_from_chat_id(
    *, runtime_root: Path, chat_id: str,
) -> SessionRepairTarget:
    try:
        target = _resolve_from_chat_id(
            runtime_root=runtime_root, chat_id=chat_id,
        )
    except NativeSessionUnavailable as exc:
        return SessionRepairTarget(None, None, str(exc))
    return SessionRepairTarget(target.source.session_id, target.source.source_label)


def _resolve_repair_from_spawn_id(
    *, project_root: Path, runtime_root: Path, spawn_id: str,
) -> SessionRepairTarget:
    try:
        target = _resolve_from_spawn_id(
            project_root=project_root, runtime_root=runtime_root, spawn_id=spawn_id,
        )
    except NativeSessionUnavailable as exc:
        return SessionRepairTarget(None, None, str(exc))
    return SessionRepairTarget(target.source.session_id, target.source.source_label)


def _resolve_repair_from_session_ref(
    *,
    project_root: Path,
    session_ref: str,
) -> SessionRepairTarget:
    target = _untracked_target(project_root=project_root, session_ref=session_ref)
    return SessionRepairTarget(target.source.session_id, target.source.source_label)


def resolve_session_repair_target(
    *,
    ref: str,
    project_root: Path,
    runtime_root: Path,
) -> SessionRepairTarget:
    normalized_ref = ref.strip()
    if not normalized_ref:
        raise ValueError("Session reference is required")
    if _is_chat_ref(runtime_root, normalized_ref):
        return _resolve_repair_from_chat_id(
            runtime_root=runtime_root,
            chat_id=normalized_ref,
        )
    if _is_spawn_ref(normalized_ref):
        return _resolve_repair_from_spawn_id(
            project_root=project_root,
            runtime_root=runtime_root,
            spawn_id=normalized_ref,
        )
    return _resolve_repair_from_session_ref(
        project_root=project_root,
        session_ref=normalized_ref,
    )


__all__ = ["SessionRepairTarget", "resolve_session_repair_target"]
