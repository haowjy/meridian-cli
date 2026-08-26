"""Fresh session re-entry policy for resume and fork decisions."""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.ops.reference import resolve_session_reference
from meridian.lib.ops.runtime import resolve_roots_for_read
from meridian.lib.state.session_store import is_session_lease_owner_alive


@dataclass(frozen=True)
class Resume:
    chat_id: str


@dataclass(frozen=True)
class Fork:
    chat_id: str


@dataclass(frozen=True)
class Blocked:
    reason: str


type SessionReentryDecision = Resume | Fork | Blocked


def decide_reentry(
    *, chat_id: str, live: bool, has_harness_session: bool
) -> SessionReentryDecision:
    """Choose the safe action from already-materialized session metadata."""

    if not has_harness_session:
        return Blocked("no harness session recorded; cannot resume or fork")
    if live:
        return Fork(chat_id)
    return Resume(chat_id)


def resolve_session_reentry(project_root: str, chat_id: str) -> SessionReentryDecision:
    """Resolve the authoritative re-entry action from fresh state reads."""

    roots = resolve_roots_for_read(project_root)
    if roots is None:
        return Blocked("session is no longer available")
    reference = resolve_session_reference(
        roots.project_root,
        chat_id,
        runtime_root=roots.runtime_root,
    )
    return decide_reentry(
        chat_id=chat_id,
        live=is_session_lease_owner_alive(roots.runtime_root, chat_id),
        has_harness_session=reference.authoritative_harness_session_id is not None,
    )


__all__ = [
    "Blocked",
    "Fork",
    "Resume",
    "SessionReentryDecision",
    "decide_reentry",
    "resolve_session_reentry",
]
