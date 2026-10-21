"""Fresh session re-entry policy for resume and fork decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from meridian.lib.ops.reference_recovery import recover_recorded_chat_harness_session_id
from meridian.lib.ops.runtime import resolve_roots_for_read
from meridian.lib.state.session_store import (
    chat_record,
    is_session_lease_owner_alive,
)


@dataclass(frozen=True)
class Resume:
    chat_id: str
    kind: Literal["resume"] = field(default="resume", init=False)


@dataclass(frozen=True)
class Fork:
    chat_id: str
    kind: Literal["fork"] = field(default="fork", init=False)


@dataclass(frozen=True)
class StartFrom:
    """Eligibility: a fresh primary session may be started with ``--from chat_id``."""

    chat_id: str
    kind: Literal["start-from"] = field(default="start-from", init=False)


@dataclass(frozen=True)
class StartFromLaunch:
    """What the picker returns for start-from: the source chat plus the launch inputs."""

    chat_id: str
    agent: str
    model: str
    work_id: str
    prompt: str
    kind: Literal["start-from"] = field(default="start-from", init=False)


@dataclass(frozen=True)
class Blocked:
    reason: str
    kind: Literal["blocked"] = field(default="blocked", init=False)


type SessionReentryDecision = Resume | Fork | Blocked
type StartFromDecision = StartFrom | Blocked
# What the picker returns and the exec handoff accepts.
type Launch = Resume | Fork | StartFromLaunch


def decide_reentry(
    *, chat_id: str, live: bool, has_harness_session: bool
) -> SessionReentryDecision:
    """Choose the safe action from already-materialized session metadata."""

    if not has_harness_session:
        return Blocked("no harness session recorded; cannot resume or fork")
    if live:
        return Fork(chat_id)
    return Resume(chat_id)


def decide_start_from(*, chat_id: str, has_harness_session: bool) -> StartFromDecision:
    """``--from`` reads recorded state only, so running, stopped and restored chats all qualify."""

    if not has_harness_session:
        return Blocked("no harness session recorded; cannot start from it")
    return StartFrom(chat_id)


def resolve_session_reentry(project_root: str, chat_id: str) -> SessionReentryDecision:
    """Resolve the authoritative re-entry action from fresh state reads."""

    roots = resolve_roots_for_read(project_root)
    if roots is None:
        return Blocked("session is no longer available")
    record = chat_record(roots.runtime_root, chat_id)
    if record is None or record.kind != "primary":
        return Blocked("session is no longer available")
    if record.record_mode == "historical":
        return Blocked("historical record; read-only, cannot resume or fork")
    if record.native_key() is None:
        return Blocked("unbound native session; cannot resume or fork")
    return decide_reentry(
        chat_id=chat_id,
        live=is_session_lease_owner_alive(roots.runtime_root, chat_id),
        has_harness_session=recover_recorded_chat_harness_session_id(
            roots.runtime_root,
            chat_id,
            session=record,
        )
        is not None,
    )


__all__ = [
    "Blocked",
    "Fork",
    "Launch",
    "Resume",
    "SessionReentryDecision",
    "StartFrom",
    "StartFromDecision",
    "StartFromLaunch",
    "decide_reentry",
    "decide_start_from",
    "resolve_session_reentry",
]
