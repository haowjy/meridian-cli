"""Collect and acknowledge background spawn results owed to an idle primary."""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.bootstrap.services import RuntimeReadContext
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.spawn_lifecycle import is_terminal_spawn_status
from meridian.lib.harness.pi_private_state import (
    ClearedSpawns,
    DeliveryReceipts,
    pi_coordination_dir,
    read_pi_private_doc,
)
from meridian.lib.ops.runtime import resolve_runtime_authority_for_read, runtime_context
from meridian.lib.state import history_index
from meridian.lib.state.history_index import HistoryIndexBusy
from meridian.lib.state.session_identity import spawn_owner_chat_id
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import SpawnStateQuarantined
from meridian.lib.state.spawn_observations import consumed_ids, leased_ids, record
from meridian.lib.state.spawn_status import read_derived

from .models import SpawnDetailOutput, task_dir_line

_COLLECT_POLL_INTERVAL_SECS = 1.0
_DELIVERY_MAX_MEMBERS = 32


class SpawnCollectInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    exclude: tuple[str, ...] = ()
    project_root: str | None = None


class SpawnCollectOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["delivery", "empty"]
    ids: tuple[str, ...]
    text: str

    def to_wire(self) -> dict[str, object]:
        return {"kind": self.kind, "ids": list(self.ids), "text": self.text}


class SpawnAckInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_ids: tuple[str, ...]
    project_root: str | None = None


class SpawnAckOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    acked: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return {"acked": list(self.acked)}


_EMPTY = SpawnCollectOutput(kind="empty", ids=(), text="")


@dataclass(frozen=True)
class OwedSpawnScope:
    """Fixed child candidates and the parents whose observations consume them."""

    parent_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True)
class OwedSnapshot:
    """Readable, unleased owed rows plus candidates that cannot settle yet."""

    rows: tuple[SpawnRecord, ...]
    pending_ids: tuple[str, ...]
    unavailable_ids: tuple[str, ...]


def render_delivery(rows: Sequence[SpawnRecord | SpawnDetailOutput]) -> str:
    """Render Claude's header-only notification for terminal child rows."""

    rendered: list[str] = []
    for row in rows:
        if isinstance(row, SpawnRecord):
            spawn_id = row.id
            terminal_present = row.terminal is not None
            duration_secs = row.terminal.duration_secs if row.terminal is not None else None
            exit_code = row.terminal.exit_code if row.terminal is not None else None
            failure = row.terminal.error if row.terminal is not None else None
            model = row.model or ""
            harness = row.harness or ""
            authority_root = row.control_root
            task_cwd = row.task_cwd
            work_id = row.work_id
        else:
            spawn_id = row.spawn_id
            terminal_present = is_terminal_spawn_status(row.status)
            duration_secs = row.duration_secs
            exit_code = row.exit_code
            failure = row.failure_reason
            model = row.model
            harness = row.harness
            authority_root = row.authority_root
            task_cwd = row.task_cwd
            work_id = row.work_id
        outcome: list[str] = []
        if duration_secs is not None:
            outcome.append(f"{duration_secs:.1f}s")
        if terminal_present:
            outcome.append(f"exit {exit_code}")
        if failure is not None:
            outcome.append("result error")
        outcome_suffix = f" ({', '.join(outcome)})" if outcome else ""

        lines = [
            f"{spawn_id} {row.status}{outcome_suffix}",
            f"Worker: {model} ({harness})",
        ]
        if failure is not None:
            lines.append(f"Failure: {failure}")
        if authority_root is not None and task_cwd is not None and task_cwd != authority_root:
            lines.append(task_dir_line(task_cwd, work_id))
        lines.append(f"meridian spawn show {spawn_id}")
        rendered.append("\n".join(lines))
    return "\n\n".join(rendered)


def select_delivery_rows(rows: Sequence[SpawnRecord]) -> tuple[SpawnRecord, ...]:
    """Take one oldest-first delivery batch under the shared member bound."""

    return tuple(rows[:_DELIVERY_MAX_MEMBERS])


def _roots(
    project_root: str | None,
    prepared: RuntimeReadContext | None,
) -> tuple[Path, Path | None]:
    if prepared is not None:
        return prepared.project_root, prepared.runtime_root
    authority = resolve_runtime_authority_for_read(project_root)
    return authority.project_root, authority.runtime_root


def owed_consumed_ids(runtime_root: Path, parent_ids: Sequence[str]) -> frozenset[str]:
    """Return every result-consumption source honored by CLI and Pi delivery."""

    consumed = set(consumed_ids(runtime_root, parent_ids))
    for parent_id in set(parent_ids):
        coordination_dir = pi_coordination_dir(runtime_root, parent_id)
        receipts = read_pi_private_doc(
            coordination_dir / "delivery-receipts.json",
            DeliveryReceipts,
            spawn_id=parent_id,
        )
        if receipts is not None:
            consumed.update(work_id for ids in receipts.messages.values() for work_id in ids)
        cleared = read_pi_private_doc(
            coordination_dir / "cleared-spawns.json",
            ClearedSpawns,
            spawn_id=parent_id,
        )
        if cleared is not None:
            consumed.update(cleared.cleared_spawn_ids)
    return frozenset(consumed)


def discover_owed_scope(
    *,
    runtime_root: Path,
    chat_id: str,
    caller_spawn_id: str,
    excluded: frozenset[str] = frozenset(),
    require_primary: bool = False,
    deadline: float | None = None,
) -> OwedSpawnScope:
    """Discover one caller's fixed set of potentially owed background children.

    A primary owns direct children across every primary generation of its chat. A
    nested caller owns only its own direct children. Authority revalidation and
    live lease subtraction happen in :func:`read_owed_snapshot`.
    """

    index = history_index.HistoryIndex(runtime_root)
    current = read_derived(runtime_root, caller_spawn_id)
    current_is_primary = (
        current is not None
        and current.record_mode == "live"
        and current.kind == "primary"
        and spawn_owner_chat_id(current) == chat_id
    )
    if require_primary and not current_is_primary:
        raise ValueError("Spawn collection caller is not this chat's primary")
    if current is None or current.record_mode != "live":
        return OwedSpawnScope(parent_ids=(), candidate_ids=())

    if current.kind == "primary":
        if not current_is_primary:
            return OwedSpawnScope(parent_ids=(), candidate_ids=())
        primary_rows = index.spawns(
            owner_chat_id=chat_id,
            kind="primary",
            oldest_first=True,
            deadline=deadline,
        )
        parent_ids = tuple(
            dict.fromkeys((*[row.id for row in primary_rows], caller_spawn_id))
        )
    else:
        parent_ids = (caller_spawn_id,)

    children = index.spawns(
        parent_id=set(parent_ids),
        oldest_first=True,
        deadline=deadline,
    )
    consumed = owed_consumed_ids(runtime_root, parent_ids)
    candidate_ids = tuple(
        dict.fromkeys(
            row.id
            for row in children
            if row.launch_mode == "background"
            and row.id not in consumed
            and row.id not in excluded
        )
    )
    return OwedSpawnScope(parent_ids=parent_ids, candidate_ids=candidate_ids)


def read_owed_snapshot(
    *,
    runtime_root: Path,
    scope: OwedSpawnScope,
) -> OwedSnapshot:
    """Re-read one fixed candidate set and subtract consumption and live leases."""

    consumed = owed_consumed_ids(runtime_root, scope.parent_ids)
    owed_ids = tuple(
        spawn_id for spawn_id in scope.candidate_ids if spawn_id not in consumed
    )
    if not owed_ids:
        return OwedSnapshot(rows=(), pending_ids=(), unavailable_ids=())
    leased = leased_ids(runtime_root, scope.parent_ids)
    rows: list[SpawnRecord] = []
    pending: list[str] = []
    unavailable: list[str] = []
    for spawn_id in owed_ids:
        if spawn_id in leased:
            pending.append(spawn_id)
            continue
        try:
            row = read_derived(runtime_root, spawn_id)
        except (OSError, SpawnStateQuarantined):
            pending.append(spawn_id)
            unavailable.append(spawn_id)
            continue
        if row is None or row.record_mode != "live":
            continue
        if row.parent_id not in scope.parent_ids or row.launch_mode != "background":
            continue
        rows.append(row)
        if not is_terminal_spawn_status(row.status):
            pending.append(spawn_id)
    return OwedSnapshot(
        rows=tuple(rows),
        pending_ids=tuple(pending),
        unavailable_ids=tuple(unavailable),
    )


def collect_owed_sync(
    payload: SpawnCollectInput,
    ctx: RuntimeContext | None = None,
    *,
    prepared: RuntimeReadContext | None = None,
) -> SpawnCollectOutput:
    """Block for the next owed delivery without consuming any result."""

    resolved_context = runtime_context(ctx)
    chat_id = (resolved_context.chat_id or "").strip()
    if not chat_id:
        raise ValueError("Spawn collection requires MERIDIAN_CHAT_ID")
    current_spawn_id = str(resolved_context.spawn_id) if resolved_context.spawn_id else None
    if current_spawn_id is None:
        raise ValueError("Spawn collection requires MERIDIAN_SPAWN_ID")
    _, runtime_root = _roots(payload.project_root, prepared)
    if runtime_root is None:
        return _EMPTY

    excluded = frozenset(spawn_id.strip() for spawn_id in payload.exclude if spawn_id.strip())
    parent_pid = os.getppid()
    scope: OwedSpawnScope | None = None
    while os.getppid() == parent_pid:
        try:
            scope = discover_owed_scope(
                runtime_root=runtime_root,
                chat_id=chat_id,
                caller_spawn_id=current_spawn_id,
                excluded=excluded,
                require_primary=True,
                deadline=time.monotonic() + _COLLECT_POLL_INTERVAL_SECS,
            )
            break
        except (HistoryIndexBusy, OSError, SpawnStateQuarantined, ValidationError):
            time.sleep(_COLLECT_POLL_INTERVAL_SECS)
    if scope is None:
        return _EMPTY

    # Set from the first snapshot: when nothing was finished yet, wait for the
    # whole owed batch instead of delivering the first child to finish.
    wait_for_batch: bool | None = None
    while os.getppid() == parent_pid:
        snapshot = read_owed_snapshot(runtime_root=runtime_root, scope=scope)
        if snapshot.unavailable_ids:
            time.sleep(_COLLECT_POLL_INTERVAL_SECS)
            continue
        if not snapshot.rows and not snapshot.pending_ids:
            return _EMPTY
        terminal = [row for row in snapshot.rows if is_terminal_spawn_status(row.status)]
        if wait_for_batch is None:
            wait_for_batch = not terminal
        if terminal and not (wait_for_batch and snapshot.pending_ids):
            delivered_rows = select_delivery_rows(terminal)
            text = render_delivery(delivered_rows)
            # An explicit wait or ack may have taken a result meanwhile.
            taken = owed_consumed_ids(runtime_root, scope.parent_ids) | leased_ids(
                runtime_root, scope.parent_ids
            )
            if not any(row.id in taken for row in delivered_rows):
                ids = tuple(row.id for row in delivered_rows)
                return SpawnCollectOutput(kind="delivery", ids=ids, text=text)
            continue
        time.sleep(_COLLECT_POLL_INTERVAL_SECS)
    return _EMPTY


def ack_owed_sync(
    payload: SpawnAckInput,
    ctx: RuntimeContext | None = None,
    *,
    prepared: RuntimeReadContext | None = None,
) -> SpawnAckOutput:
    """Durably consume delivered ids under the caller's primary generation."""

    resolved_context = runtime_context(ctx)
    parent_spawn_id = str(resolved_context.spawn_id) if resolved_context.spawn_id else None
    if parent_spawn_id is None:
        raise ValueError("Spawn acknowledgement requires MERIDIAN_SPAWN_ID")
    _, runtime_root = _roots(payload.project_root, prepared)
    if runtime_root is None:
        raise ValueError("Project has no runtime state")
    acked = tuple(
        dict.fromkeys(
            spawn_id.strip() for spawn_id in payload.spawn_ids if spawn_id.strip()
        )
    )
    if not record(
        runtime_root=runtime_root,
        parent_spawn_id=parent_spawn_id,
        observed_add=acked,
    ):
        raise ValueError(f"Spawn acknowledgement: spawn '{parent_spawn_id}' is not published")
    return SpawnAckOutput(acked=acked)


__all__ = [
    "SpawnAckInput",
    "SpawnAckOutput",
    "SpawnCollectInput",
    "SpawnCollectOutput",
    "ack_owed_sync",
    "collect_owed_sync",
    "discover_owed_scope",
    "owed_consumed_ids",
    "read_owed_snapshot",
    "render_delivery",
    "select_delivery_rows",
]
