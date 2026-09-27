"""Bind an unbound chat to its native session: inspect candidates, then bind explicitly.

``meridian session repair cN`` is read-only: it lists candidate native files
with their evidence. ``--native PATH`` binds after validation through the one
binding rule, with source ``user_repair``. Bindings are immutable, so a bound
chat is never rebound.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.core.native_identity import NativeKeyFields
from meridian.lib.core.util import FormatContext
from meridian.lib.harness.legacy_native_stores import (
    InvalidNativeSession,
    LegacyNativeStores,
    NativeSessionEvidence,
    claude_sessions_in,
    native_session_evidence,
    pi_sessions_in,
    pi_shared_session_root,
    pi_spawn_session_dir,
)
from meridian.lib.ops.legacy_native_import import (
    RetainedChatFacts,
    configured_archive_destination,
    retained_chat_facts,
)
from meridian.lib.ops.runtime import async_from_sync, resolve_runtime_root_for_read
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.native_binding import Conflict
from meridian.lib.state.session_binding import session_bindings

MAX_LISTED = 20
EXCERPT = 160


class SessionRepairInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str = ""
    native: str | None = None
    force: bool = False
    project_root: str | None = None


class RepairCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    session_id: str
    cwd: str | None
    started_at: str | None
    first_user: str | None
    cwd_match: bool
    time_match: bool
    prompt_match: bool | None
    bound_to: str | None
    bind_command: str | None


class SessionRepairOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat_id: str
    harness: str
    kind: str
    action: Literal["already_bound", "inspect", "bound"]
    binding: dict[str, str | None] | None = None
    searched: tuple[str, ...] = ()
    candidates: tuple[RepairCandidate, ...] = ()
    omitted: int = 0
    forced: tuple[str, ...] = ()
    note: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if self.action == "already_bound":
            key = self.binding or {}
            return (
                f"{self.chat_id} is bound to {self.harness} session {key.get('session_id')} "
                f"in {key.get('native_store')}. Nothing to repair."
            )
        if self.action == "bound":
            key = self.binding or {}
            forced = f" (forced past: {', '.join(self.forced)})" if self.forced else ""
            return (
                f"Bound {self.chat_id} to {self.harness} session {key.get('session_id')} "
                f"in {key.get('native_store')}{forced}."
            )
        lines = [f"{self.chat_id} ({self.harness} {self.kind}) is unbound."]
        if self.searched:
            lines.append("Searched: " + ", ".join(self.searched))
        if self.note:
            lines.append(self.note)
        if not self.candidates:
            lines.append("No candidate native sessions found.")
            lines.append(
                f"Bind a file you located yourself: meridian session repair {self.chat_id} "
                "--native <path>"
            )
            return "\n".join(lines)
        lines.append(f"{len(self.candidates)} candidate native session(s):")
        for candidate in self.candidates:
            prompt = {True: "yes", False: "no", None: "n/a"}[candidate.prompt_match]
            lines.extend(
                [
                    "",
                    f"  {candidate.path}",
                    f"    session {candidate.session_id}  started {candidate.started_at or '?'}",
                    f"    cwd {candidate.cwd or '?'}",
                    f"    first user message: {candidate.first_user or '(none)'}",
                    f"    matches: cwd {'yes' if candidate.cwd_match else 'no'} · "
                    f"time window {'yes' if candidate.time_match else 'no'} · prompt {prompt} · "
                    f"bound elsewhere {candidate.bound_to or 'no'}",
                    f"    bind: {candidate.bind_command or 'refused (bound to another chat)'}",
                ]
            )
        if self.omitted:
            lines.append(f"\n({self.omitted} more candidate(s) not shown.)")
        return "\n".join(lines)


def _excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= EXCERPT else flat[: EXCERPT - 1] + "…"


def _chat_id_for(runtime_root: Path, ref: str) -> str:
    if ref.startswith("c") and ref[1:].isdigit():
        return ref
    if ref.startswith("p") and ref[1:].isdigit():
        spawn = spawn_store.get_spawn(runtime_root, ref)
        if spawn is not None and spawn.chat_id:
            return str(spawn.chat_id)
        for record in session_store.list_all_session_records(runtime_root):
            if record.spawn_id == ref:
                return str(record.chat_id)
        raise ValueError(f"Spawn {ref} has no recorded chat.")
    raise ValueError("session repair takes a chat (c123) or spawn (p123) reference.")


def _candidate_dirs(record: session_store.SessionRecord, fact: RetainedChatFacts) -> list[Path]:
    """Where a cheap, exact candidate listing exists; empty means --native only."""
    if record.harness == "pi":
        directories = set(fact.session_dirs)
        if record.kind == "primary":
            directories.add(pi_shared_session_root())
        else:
            directories |= {pi_spawn_session_dir(spawn_id) for spawn_id in fact.spawn_ids}
        return sorted(directories)
    if record.harness == "claude":
        if record.native_store:
            return [Path(record.native_store)]
        stores = LegacyNativeStores().candidates(record, [], {Path(cwd) for cwd in fact.cwds})
        return sorted(stores)
    return []


def _bound_elsewhere(
    records: dict[str, session_store.SessionRecord], harness: str, chat_id: str
) -> dict[str, str]:
    return {
        str(record.harness_session_id): other
        for other, record in records.items()
        if other != chat_id and record.harness == harness and record.harness_session_id
    }


def _mismatches(fact: RetainedChatFacts, evidence: NativeSessionEvidence) -> list[str]:
    reasons: list[str] = []
    if not fact.cwd_matches(evidence.cwd):
        reasons.append(f"cwd mismatch (session cwd {evidence.cwd or 'unknown'})")
    if not fact.time_matches(evidence.started_at):
        started = evidence.started_at.isoformat() if evidence.started_at else "unknown"
        reasons.append(f"outside the time window (session started {started})")
    return reasons


def repair_session_reference_sync(payload: SessionRepairInput) -> SessionRepairOutput:
    ref = payload.ref.strip()
    if not ref:
        raise ValueError("session repair requires a chat or spawn reference")
    explicit_root = (
        Path(payload.project_root).expanduser().resolve() if payload.project_root else None
    )
    resolution = resolve_project_root_resolution(explicit_root)
    runtime_root = resolve_runtime_root_for_read(resolution.project_root)
    if runtime_root is None:
        raise ValueError(f"Session reference '{ref}' not found.")
    chat_id = _chat_id_for(runtime_root, ref)
    records = {str(r.chat_id): r for r in session_store.list_all_session_records(runtime_root)}
    record = records.get(chat_id)
    if record is None:
        raise ValueError(f"Chat {chat_id} not found.")
    key = record.native_key()
    if key is not None:
        if payload.native:
            raise ValueError(
                f"Refusing: {chat_id} is already bound to {record.harness} session "
                f"{key.session_id}; bindings are immutable."
            )
        return SessionRepairOutput(
            chat_id=chat_id,
            harness=record.harness,
            kind=record.kind,
            action="already_bound",
            binding=record.key_fields().render(),
        )

    fact = retained_chat_facts(
        runtime_root,
        {chat_id: record},
        archive_destination=configured_archive_destination(resolution.project_root),
    )[chat_id]
    elsewhere = _bound_elsewhere(records, record.harness, chat_id)
    if payload.native:
        return _bind(
            runtime_root, record, fact, Path(payload.native).expanduser(), payload.force, elsewhere
        )

    directories = _candidate_dirs(record, fact)
    found: list[NativeSessionEvidence] = []
    for directory in directories:
        found.extend(
            pi_sessions_in(directory) if record.harness == "pi" else claude_sessions_in(directory)
        )
    candidates = [
        RepairCandidate(
            path=str(evidence.path),
            session_id=evidence.session_id,
            cwd=evidence.cwd,
            started_at=evidence.started_at.isoformat() if evidence.started_at else None,
            first_user=_excerpt(evidence.first_user),
            cwd_match=fact.cwd_matches(evidence.cwd),
            time_match=fact.time_matches(evidence.started_at),
            prompt_match=fact.prompt_matches(evidence),
            bound_to=elsewhere.get(evidence.session_id),
            bind_command=None
            if evidence.session_id in elsewhere
            else shlex.join(
                [
                    "meridian",
                    "session",
                    "repair",
                    chat_id,
                    "--native",
                    str(evidence.path),
                    *(["--force"] if _mismatches(fact, evidence) else []),
                ]
            ),
        )
        for evidence in found
    ]
    candidates.sort(
        key=lambda c: (
            c.bound_to is not None,
            -(c.cwd_match + c.time_match + bool(c.prompt_match)),
            c.started_at or "",
        )
    )
    # A shared root holds every project's sessions; show the plausible ones.
    plausible = [c for c in candidates if c.cwd_match or c.time_match]
    unrelated = len(candidates) - len(plausible) if plausible else 0
    candidates = plausible or candidates
    note = (
        f"{unrelated} other session(s) there match neither the chat's cwd nor its time window."
        if unrelated
        else None
    )
    if not directories:
        note = (
            "No spawn session directory is recorded for this chat."
            if record.harness == "pi"
            else f"Candidate listing is not available for {record.harness}; pass --native <path>."
        )
    return SessionRepairOutput(
        chat_id=chat_id,
        harness=record.harness,
        kind=record.kind,
        action="inspect",
        searched=tuple(str(directory) for directory in directories),
        candidates=tuple(candidates[:MAX_LISTED]),
        omitted=max(0, len(candidates) - MAX_LISTED),
        note=note,
    )


def _bind(
    runtime_root: Path,
    record: session_store.SessionRecord,
    fact: RetainedChatFacts,
    path: Path,
    force: bool,
    elsewhere: dict[str, str],
) -> SessionRepairOutput:
    chat_id = str(record.chat_id)
    try:
        evidence = native_session_evidence(
            record.harness, path, recorded_session_id=record.harness_session_id
        )
    except InvalidNativeSession as exc:
        raise ValueError(f"Refusing: {exc}") from exc
    if record.harness_session_id and record.harness_session_id != evidence.session_id:
        raise ValueError(
            f"Refusing: {chat_id} already records session ID {record.harness_session_id}, "
            f"not {evidence.session_id}; bindings are immutable."
        )
    if evidence.session_id in elsewhere:
        raise ValueError(
            f"Refusing: {record.harness} session {evidence.session_id} is already bound "
            f"to {elsewhere[evidence.session_id]}."
        )
    mismatches = _mismatches(fact, evidence)
    if mismatches and not force:
        raise ValueError(
            f"Refusing to bind {chat_id} to {evidence.path}: "
            + "; ".join(mismatches)
            + ". Re-run with --force if you are sure this is the chat's session."
        )
    attempted = NativeKeyFields(record.harness, str(evidence.native_store), evidence.session_id)
    with session_bindings(runtime_root) as bindings:
        current = bindings.records.get(chat_id)
        if current is None or current.native_key() is not None:
            raise ValueError(f"Refusing: {chat_id} is already bound; bindings are immutable.")
        owner = next(
            (
                other
                for other, row in bindings.records.items()
                if other != chat_id
                and row.harness == record.harness
                and row.harness_session_id == evidence.session_id
            ),
            None,
        )
        if owner is not None:
            raise ValueError(
                f"Refusing: {record.harness} session {evidence.session_id} is already bound "
                f"to {owner}."
            )
        result = bindings.bind(
            chat_id,
            attempted,
            source="user_repair",
            session_instance_id=current.session_instance_id,
        )
        if isinstance(result, Conflict):
            raise ValueError(
                f"Refusing: {chat_id}'s recorded {result.field} conflicts with {evidence.path}."
            )
    return SessionRepairOutput(
        chat_id=chat_id,
        harness=record.harness,
        kind=record.kind,
        action="bound",
        binding=attempted.render(),
        forced=tuple(mismatches),
    )


session_repair = async_from_sync(repair_session_reference_sync)


__all__ = [
    "RepairCandidate",
    "SessionRepairInput",
    "SessionRepairOutput",
    "repair_session_reference_sync",
    "session_repair",
]
