"""Collect and acknowledge background spawn results owed to an idle primary."""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict

from meridian.lib.bootstrap.services import RuntimeReadContext
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.logging import warn_once
from meridian.lib.core.spawn_lifecycle import is_terminal_spawn_status
from meridian.lib.harness.pi_private_state import (
    read_pi_consumption_state,
)
from meridian.lib.ops.runtime import resolve_runtime_authority_for_read, runtime_context
from meridian.lib.state import history_index
from meridian.lib.state.history_index import HistoryIndexBusy
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import SpawnStateQuarantined
from meridian.lib.state.spawn_observations import (
    LaunchKey,
    delivered_launches,
    leased_launches,
)
from meridian.lib.state.spawn_observations import record as record_spawn_observation
from meridian.lib.state.spawn_ownership import (
    is_live_chat_primary,
    is_owed_child,
    owed_launches,
    owned_parent_ids,
)
from meridian.lib.state.spawn_status import read_derived, read_launch

from .models import SpawnDetailOutput, render_delivery
from .query import detail_from_row

_COLLECT_POLL_INTERVAL_SECS = 1.0
_DELIVERY_MAX_MEMBERS = 32
_OBSERVATION_LOCK_TIMEOUT_SECS = 1.0
logger = structlog.get_logger(__name__)


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
    """Fixed launch candidates and the parents whose observations consume them."""

    parent_ids: tuple[str, ...]
    candidates: tuple[LaunchKey, ...]


@dataclass(frozen=True)
class OwedSnapshot:
    """Readable, unleased owed rows plus candidates that cannot settle yet.

    A row is one launch's: the spawn's current row, or the archive of a launch a continue
    moved past.
    """

    rows: tuple[SpawnRecord, ...]
    pending_keys: tuple[LaunchKey, ...]
    unavailable_keys: tuple[LaunchKey, ...]


def delivery_detail(
    *, project_root: Path, runtime_root: Path, row: SpawnRecord
) -> SpawnDetailOutput:
    """A row's detail for ``render_delivery``, with its report when it is a past run.

    Once a continue starts the next launch, ``spawn show`` and ``spawn report show`` describe
    that one, so a delivered earlier launch carries its own report.
    """

    moved = (current := read_derived(runtime_root, row.id)) is not None and (
        current.launch_number != row.launch_number
    )
    detail = detail_from_row(
        project_root=project_root,
        row=row,
        include_report_body=moved,
        runtime_root=runtime_root,
    )
    if moved and detail.report_body is None:
        return detail.model_copy(update={"report_body": "(no report was written)"})
    return detail


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
    """Return every result-consumption source honored by CLI and Pi delivery, as launch keys.

    The one union reader: its TS twin is ``readConsumption`` in the spawn-watch extension.
    """

    consumed: set[str] = set()

    def diagnose(file: Path, error: str | None) -> None:
        if error is not None:
            warn_once(
                ("pi_consumption_source_skipped", file, error),
                lambda: logger.warning(
                    "pi_consumption_source_skipped", path=str(file), error=error
                ),
            )

    for parent_id in set(parent_ids):
        consumption = read_pi_consumption_state(
            runtime_root,
            parent_id,
            on_read=diagnose,
        )
        consumed.update(consumption.consumed_ids)
    return frozenset(consumed)


def discover_owed_scope(
    *,
    runtime_root: Path,
    chat_id: str,
    caller_spawn_id: str,
    parent_ids: tuple[str, ...] | None = None,
    excluded: frozenset[str] = frozenset(),
    require_primary: bool = False,
    deadline: float | None = None,
) -> OwedSpawnScope:
    """Discover one caller's fixed set of potentially owed background launches.

    Candidates are the parents' current background children plus the launches a continue
    moved past while still owed (the index's ``owed_launches`` side table), whichever parent
    the spawn belongs to now. ``excluded`` holds spawn IDs: every launch of one is excluded.

    A primary owns direct children across every primary generation of its chat; a
    nested caller owns only its own direct children (see
    :func:`meridian.lib.state.spawn_ownership.owned_parent_ids`). Authority
    revalidation and live lease subtraction happen in :func:`read_owed_snapshot`.
    A caller that already computed ``parent_ids`` passes them so both share one root set.
    """

    current = read_derived(runtime_root, caller_spawn_id)
    if require_primary and not is_live_chat_primary(current, chat_id=chat_id):
        raise ValueError("Spawn collection caller is not this chat's primary")
    if parent_ids is None:
        parent_ids = owned_parent_ids(
            runtime_root,
            current,
            chat_id=chat_id,
            caller_spawn_id=caller_spawn_id,
            deadline=deadline,
        )
    if not parent_ids:
        return OwedSpawnScope(parent_ids=(), candidates=())

    owed = owed_launches(history_index.HistoryIndex(runtime_root), parent_ids, deadline=deadline)
    consumed = owed_consumed_ids(runtime_root, parent_ids)
    candidates = tuple(
        key for key in owed if str(key) not in consumed and key.spawn_id not in excluded
    )
    return OwedSpawnScope(parent_ids=parent_ids, candidates=candidates)


def read_owed_snapshot(
    *,
    runtime_root: Path,
    scope: OwedSpawnScope,
) -> OwedSnapshot:
    """Re-read one fixed candidate set and subtract consumption and live leases."""

    consumed = owed_consumed_ids(runtime_root, scope.parent_ids)
    owed = tuple(key for key in scope.candidates if str(key) not in consumed)
    if not owed:
        return OwedSnapshot(rows=(), pending_keys=(), unavailable_keys=())
    leased = leased_launches(runtime_root, scope.parent_ids)
    rows: list[SpawnRecord] = []
    pending: list[LaunchKey] = []
    unavailable: list[LaunchKey] = []
    for key in owed:
        if str(key) in leased:
            pending.append(key)
            continue
        try:
            row = read_launch(runtime_root, *key)
        except (OSError, SpawnStateQuarantined):
            pending.append(key)
            unavailable.append(key)
            continue
        if row is None or row.record_mode != "live":
            continue
        if row.parent_id not in scope.parent_ids or not is_owed_child(row):
            continue
        rows.append(row)
        if not is_terminal_spawn_status(row.status):
            pending.append(key)
    return OwedSnapshot(
        rows=tuple(rows),
        pending_keys=tuple(pending),
        unavailable_keys=tuple(unavailable),
    )


def resolve_owed_finished_rows(
    *,
    runtime_root: Path,
    chat_id: str,
    caller_spawn_id: str,
    parent_ids: tuple[str, ...],
    excluded_refs: Collection[LaunchKey],
    deadline: float | None = None,
) -> tuple[SpawnRecord, ...]:
    """Resolve every finished owed row for no-argument wait, oldest first, uncapped."""

    scope = discover_owed_scope(
        runtime_root=runtime_root,
        chat_id=chat_id,
        caller_spawn_id=caller_spawn_id,
        parent_ids=parent_ids,
        deadline=deadline,
    )
    snapshot = read_owed_snapshot(runtime_root=runtime_root, scope=scope)
    return tuple(
        row
        for row in snapshot.rows
        if LaunchKey.of(row) not in excluded_refs and is_terminal_spawn_status(row.status)
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
    project_root, runtime_root = _roots(payload.project_root, prepared)
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
        except (HistoryIndexBusy, OSError, SpawnStateQuarantined):
            time.sleep(_COLLECT_POLL_INTERVAL_SECS)
    if scope is None:
        return _EMPTY

    # Set from the first snapshot: when nothing was finished yet, wait for the
    # whole owed batch instead of delivering the first child to finish.
    wait_for_batch: bool | None = None
    while os.getppid() == parent_pid:
        snapshot = read_owed_snapshot(runtime_root=runtime_root, scope=scope)
        if snapshot.unavailable_keys:
            time.sleep(_COLLECT_POLL_INTERVAL_SECS)
            continue
        if not snapshot.rows and not snapshot.pending_keys:
            return _EMPTY
        terminal = [row for row in snapshot.rows if is_terminal_spawn_status(row.status)]
        if wait_for_batch is None:
            wait_for_batch = not terminal
        if terminal and not (wait_for_batch and snapshot.pending_keys):
            delivered_rows = select_delivery_rows(terminal)
            delivered_details = tuple(
                delivery_detail(project_root=project_root, runtime_root=runtime_root, row=row)
                for row in delivered_rows
            )
            text = render_delivery(delivered_details)
            # An explicit wait or ack may have taken a result meanwhile.
            taken = owed_consumed_ids(runtime_root, scope.parent_ids) | leased_launches(
                runtime_root, scope.parent_ids
            )
            keys = tuple(LaunchKey.of(row) for row in delivered_rows)
            if not any(str(key) in taken for key in keys):
                delivered: dict[str, list[int]] = {}
                for key in keys:
                    delivered.setdefault(key.spawn_id, []).append(key.n)
                _record_best_effort(runtime_root, current_spawn_id, delivered_set=delivered)
                return SpawnCollectOutput(kind="delivery", ids=tuple(delivered), text=text)
            continue
        time.sleep(_COLLECT_POLL_INTERVAL_SECS)
    return _EMPTY


def _record_best_effort(
    runtime_root: Path, caller_spawn_id: str | None, **changes: Any
) -> None:
    """Write to the caller's ledger; a failure costs a repeated delivery, never a lost one."""

    try:
        recorded = record_spawn_observation(
            runtime_root=runtime_root,
            parent_spawn_id=caller_spawn_id,
            lock_timeout=_OBSERVATION_LOCK_TIMEOUT_SECS,
            **changes,
        )
    except OSError as exc:
        log = (
            logger.debug
            if isinstance(exc, PermissionError) or exc.errno == errno.EROFS
            else logger.warning
        )
        log(
            "spawn_result_consumption_failed",
            caller_spawn_id=caller_spawn_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return
    if not recorded:
        logger.warning("spawn_result_consumption_not_recorded", caller_spawn_id=caller_spawn_id)


def record_result_shown_to_caller(
    *,
    runtime_root: Path | None,
    caller: RuntimeContext,
    spawn_id: str,
    launch_number: int,
) -> None:
    """Best-effort consumption for a launch's result shown to a calling spawn."""

    if runtime_root is None:
        return
    _record_best_effort(
        runtime_root,
        str(caller.spawn_id) if caller.spawn_id else None,
        observed_add=(str(LaunchKey(spawn_id, launch_number)),),
    )


def consume_result_if_shown(
    *,
    observe: bool,
    runtime_root: Path | None,
    caller: RuntimeContext,
    spawn_id: str,
    launch_number: int,
    status: str,
    body: str | None,
) -> None:
    """Consume a non-empty terminal result returned to an observing caller."""

    if observe and is_terminal_spawn_status(status) and (body or "").strip():
        record_result_shown_to_caller(
            runtime_root=runtime_root,
            caller=caller,
            spawn_id=spawn_id,
            launch_number=launch_number,
        )


def _finished_owed_keys(
    runtime_root: Path, context: RuntimeContext, caller_spawn_id: str, spawn_ids: Collection[str]
) -> tuple[str, ...]:
    """Ledger keys of the owed, unconsumed launches of ``spawn_ids`` that have finished.

    A launch still running is never included: acking it would consume a result nobody saw.
    """

    if not spawn_ids:
        return ()
    scope = discover_owed_scope(
        runtime_root=runtime_root,
        chat_id=(context.chat_id or "").strip(),
        caller_spawn_id=caller_spawn_id,
    )
    finished: list[str] = []
    for key in scope.candidates:
        if key.spawn_id not in spawn_ids:
            continue
        row = read_launch(runtime_root, *key)
        if row is not None and is_terminal_spawn_status(row.status):
            finished.append(str(key))
    return tuple(finished)


def ack_owed_sync(
    payload: SpawnAckInput,
    ctx: RuntimeContext | None = None,
    *,
    prepared: RuntimeReadContext | None = None,
) -> SpawnAckOutput:
    """Durably consume delivered launches under the caller's primary generation.

    IDs are the spawn IDs ``spawn collect`` returned. Each consumes exactly the launches that
    delivery recorded; with no record (a person running ``spawn ack``), the spawn's finished,
    owed, unconsumed launches. Never a launch still running.
    """

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
    unrecorded = frozenset(acked) - delivered_launches(runtime_root, parent_spawn_id).keys()
    recorded = record_spawn_observation(
        runtime_root=runtime_root,
        parent_spawn_id=parent_spawn_id,
        observed_add=_finished_owed_keys(
            runtime_root, resolved_context, parent_spawn_id, unrecorded
        ),
        ack_delivered=acked,
    )
    if not recorded:
        raise ValueError(f"Spawn acknowledgement: spawn '{parent_spawn_id}' is not published")
    return SpawnAckOutput(acked=acked)


__all__ = [
    "SpawnAckInput",
    "SpawnAckOutput",
    "SpawnCollectInput",
    "SpawnCollectOutput",
    "ack_owed_sync",
    "collect_owed_sync",
    "consume_result_if_shown",
    "delivery_detail",
    "discover_owed_scope",
    "owed_consumed_ids",
    "read_owed_snapshot",
    "record_result_shown_to_caller",
    "resolve_owed_finished_rows",
    "select_delivery_rows",
]
