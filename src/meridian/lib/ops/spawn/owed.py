"""Collect and acknowledge background spawn results owed to an idle primary."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.bootstrap.services import RuntimeReadContext
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.spawn_lifecycle import is_terminal_spawn_status
from meridian.lib.ops.runtime import resolve_runtime_authority_for_read, runtime_context
from meridian.lib.state import history_index
from meridian.lib.state.history_index import HistoryIndexBusy
from meridian.lib.state.session_identity import spawn_owner_chat_id
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import SpawnStateQuarantined
from meridian.lib.state.spawn_observations import consumed_ids, leased_ids, record
from meridian.lib.state.spawn_status import read_derived

from .models import render_delivery
from .query import detail_from_row

_COLLECT_POLL_INTERVAL_SECS = 1.0
_REPORT_READ_MAX_BYTES = 64 * 1024
# One delivery's limits, matching Pi's (pi_runtime/.context/delivery-contract.md).
_DELIVERY_MAX_MEMBERS = 32
_DELIVERY_MAX_BYTES = 64 * 1024


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


class _CollectorParentChanged(Exception):
    pass


@dataclass(frozen=True)
class _OwedSnapshot:
    """Unleased owed rows (any status) and the ids still running or leased."""

    rows: tuple[SpawnRecord, ...]
    pending_ids: tuple[str, ...]


def _roots(
    project_root: str | None,
    prepared: RuntimeReadContext | None,
) -> tuple[Path, Path | None]:
    if prepared is not None:
        return prepared.project_root, prepared.runtime_root
    authority = resolve_runtime_authority_for_read(project_root)
    return authority.project_root, authority.runtime_root


def _discover_owed_candidates(
    *,
    runtime_root: Path,
    chat_id: str,
    current_spawn_id: str,
    excluded: frozenset[str],
    parent_pid: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return all chat primaries and their projected, unconsumed background children."""

    index = history_index.HistoryIndex(runtime_root)
    while os.getppid() == parent_pid:
        try:
            deadline = time.monotonic() + _COLLECT_POLL_INTERVAL_SECS
            primary_rows = index.spawns(
                owner_chat_id=chat_id,
                kind="primary",
                oldest_first=True,
                deadline=deadline,
            )
            current = read_derived(runtime_root, current_spawn_id)
            if (
                current is None
                or current.kind != "primary"
                or spawn_owner_chat_id(current) != chat_id
            ):
                raise ValueError("Spawn collection caller is not this chat's primary")
            primary_ids = tuple(
                dict.fromkeys((*[row.id for row in primary_rows], current_spawn_id))
            )
            children = index.spawns(
                parent_id=set(primary_ids),
                oldest_first=True,
                deadline=time.monotonic() + _COLLECT_POLL_INTERVAL_SECS,
            )
            consumed = consumed_ids(runtime_root, primary_ids)
            candidate_ids = tuple(
                dict.fromkeys(
                    row.id
                    for row in children
                    if row.launch_mode == "background"
                    and row.id not in consumed
                    and row.id not in excluded
                )
            )
            return primary_ids, candidate_ids
        except (HistoryIndexBusy, ValidationError):
            time.sleep(_COLLECT_POLL_INTERVAL_SECS)
    raise _CollectorParentChanged


def _authoritative_snapshot(
    *,
    runtime_root: Path,
    primary_ids: tuple[str, ...],
    candidate_ids: tuple[str, ...],
    parent_pid: int,
) -> _OwedSnapshot:
    """Read only still-owed candidates; malformed owned rows keep the batch pending."""

    while os.getppid() == parent_pid:
        consumed = consumed_ids(runtime_root, primary_ids)
        owed_ids = tuple(spawn_id for spawn_id in candidate_ids if spawn_id not in consumed)
        if not owed_ids:
            return _OwedSnapshot(rows=(), pending_ids=())
        leased = leased_ids(runtime_root, primary_ids)
        rows: list[SpawnRecord] = []
        pending: list[str] = []
        try:
            for spawn_id in owed_ids:
                if spawn_id in leased:
                    pending.append(spawn_id)
                    continue
                row = read_derived(runtime_root, spawn_id)
                if row is None:
                    continue
                if row.parent_id not in primary_ids or row.launch_mode != "background":
                    continue
                rows.append(row)
                if not is_terminal_spawn_status(row.status):
                    pending.append(spawn_id)
            return _OwedSnapshot(rows=tuple(rows), pending_ids=tuple(pending))
        except (OSError, SpawnStateQuarantined):
            time.sleep(_COLLECT_POLL_INTERVAL_SECS)
    raise _CollectorParentChanged


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
    project_root, runtime_root = _roots(payload.project_root, prepared)
    if runtime_root is None:
        return _EMPTY

    excluded = frozenset(spawn_id.strip() for spawn_id in payload.exclude if spawn_id.strip())
    parent_pid = os.getppid()
    try:
        primary_ids, candidate_ids = _discover_owed_candidates(
            runtime_root=runtime_root,
            chat_id=chat_id,
            current_spawn_id=current_spawn_id,
            excluded=excluded,
            parent_pid=parent_pid,
        )
        # Set from the first snapshot: when nothing was finished yet, wait for the
        # whole owed batch instead of delivering the first child to finish.
        wait_for_batch: bool | None = None
        while os.getppid() == parent_pid:
            snapshot = _authoritative_snapshot(
                runtime_root=runtime_root,
                primary_ids=primary_ids,
                candidate_ids=candidate_ids,
                parent_pid=parent_pid,
            )
            if not snapshot.rows and not snapshot.pending_ids:
                return _EMPTY
            terminal = [row for row in snapshot.rows if is_terminal_spawn_status(row.status)]
            if wait_for_batch is None:
                wait_for_batch = not terminal
            if terminal and not (wait_for_batch and snapshot.pending_ids):
                details = tuple(
                    detail_from_row(
                        project_root=project_root,
                        row=row,
                        include_report_body=True,
                        runtime_root=runtime_root,
                        report_body_max_bytes=_REPORT_READ_MAX_BYTES,
                    )
                    for row in terminal[:_DELIVERY_MAX_MEMBERS]
                )
                text = render_delivery(details, max_bytes=_DELIVERY_MAX_BYTES)
                # An explicit wait or ack may have taken a result while it was read.
                taken = consumed_ids(runtime_root, primary_ids) | leased_ids(
                    runtime_root, primary_ids
                )
                if not any(detail.spawn_id in taken for detail in details):
                    ids = tuple(detail.spawn_id for detail in details)
                    return SpawnCollectOutput(kind="delivery", ids=ids, text=text)
                continue
            time.sleep(_COLLECT_POLL_INTERVAL_SECS)
    except _CollectorParentChanged:
        pass
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
]
