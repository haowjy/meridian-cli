"""Spawn reconciliation authorized by runner-lifetime launch locks."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

import structlog

from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots
from meridian.lib.core.depth import is_root_side_effect_process
from meridian.lib.core.spawn_lifecycle import (
    is_active_spawn_status,
    is_terminal_spawn_status,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.locking import lock_file
from meridian.lib.platform.process_scope import is_pgid_reachable
from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
from meridian.lib.state.launch_lock import Busy, Contended, NotFound, acquire
from meridian.lib.state.liveness import is_process_alive, is_process_dead_with_birth
from meridian.lib.state.managed_primary import is_managed_primary_candidate
from meridian.lib.state.primary_meta import PrimaryMetadata, read_primary_metadata
from meridian.lib.state.process_scope_projection import mark_scope_released, read_scopes_from_disk
from meridian.lib.state.reaper_cleanup_claim import (
    claim_active_spawn_scopes,
    cleanup_claim_path,
    cleanup_lock_path,
    read_cleanup_claim,
    replace_cleanup_claim,
)
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import (
    SpawnStateQuarantined,
    iter_spawn_ids,
    read_state,
)
from meridian.lib.state.spawn_status import claim_dead_launch

logger = structlog.get_logger(__name__)


class ScopeLiveness(TypedDict):
    """Best-effort liveness projection for a process containment scope."""

    root_alive: bool
    pgid_reachable: bool | None
    likely_serving: bool


def scope_liveness(scope: ProcessScopeSnapshot) -> ScopeLiveness:
    """Project root identity and group reachability into one diagnostic view."""

    root_alive = is_process_alive(
        scope.root_pid,
        created_after_epoch=(scope.root_created_at_epoch or None),
    )
    pgid_reachable = is_pgid_reachable(scope.pgid) if scope.pgid is not None else None
    return {
        "root_alive": root_alive,
        "pgid_reachable": pgid_reachable,
        "likely_serving": root_alive or pgid_reachable is True,
    }


def _fallback_cleanup_scopes(
    runtime_root: Path,
    record: SpawnRecord,
    primary_metadata: PrimaryMetadata | None,
) -> tuple[ProcessScopeSnapshot, ...]:
    if read_scopes_from_disk(runtime_root, SpawnId(record.id)):
        return ()
    if primary_metadata is not None and primary_metadata.managed_backend:
        scopes: list[ProcessScopeSnapshot] = []
        for scope_id, pid, birth_epoch in (
            (
                "managed-backend",
                primary_metadata.backend_pid,
                primary_metadata.backend_birth_epoch,
            ),
            ("managed-tui", primary_metadata.tui_pid, primary_metadata.tui_birth_epoch),
        ):
            if pid is None:
                continue
            scopes.append(
                ProcessScopeSnapshot(
                    scope_id=scope_id,
                    owner_policy="spawn_owned",
                    owner_id=record.id,
                    role="harness_backend",
                    containment="pid_tree_fallback",
                    root_pid=pid,
                    root_created_at_epoch=birth_epoch or 0.0,
                    pgid=None,
                    job_name=None,
                    degraded_reason="managed_primary_metadata_fallback",
                )
            )
        return tuple(scopes)
    if record.worker_pid is None or record.worker_pid <= 0:
        return ()
    return (
        ProcessScopeSnapshot(
            scope_id="legacy-worker",
            owner_policy="spawn_owned",
            owner_id=record.id,
            role="tool_worker",
            containment="pid_tree_fallback",
            root_pid=record.worker_pid,
            root_created_at_epoch=0.0,
            pgid=None,
            job_name=None,
            degraded_reason="legacy_worker_fallback",
        ),
    )


def _claim_reaper_cleanup(
    runtime_root: Path,
    record: SpawnRecord,
    primary_metadata: PrimaryMetadata | None = None,
) -> None:
    """Persist exact cleanup targets before attempting the terminal CAS."""
    claim_active_spawn_scopes(
        runtime_root,
        SpawnId(record.id),
        extra_scopes=_fallback_cleanup_scopes(runtime_root, record, primary_metadata),
    )


def _cleanup_claimed_scopes(runtime_root: Path, record: SpawnRecord) -> None:
    """Resolve a durable claim once, retaining failed targets for a later pass."""
    if record.kind == "primary" and not is_process_dead_with_birth(
        record.runner_pid, record.runner_created_at_epoch
    ):
        return
    spawn_id = SpawnId(record.id)
    with lock_file(cleanup_lock_path(runtime_root, spawn_id), reentrant=False):
        claimed = read_cleanup_claim(runtime_root, spawn_id)
        if not claimed:
            return
        if record.terminal is None or record.terminal.origin != "reconciler":
            replace_cleanup_claim(runtime_root, spawn_id, [])
            return

        from meridian.lib.core.process_cleanup import terminate_scope_sync

        unresolved: list[ProcessScopeSnapshot] = []
        for scope in claimed:
            result = terminate_scope_sync(scope, grace_seconds=5.0, reason="reaper")
            if result.skip_reason == "termination_exception":
                unresolved.append(scope)
                continue
            mark_scope_released(runtime_root, spawn_id, scope.release_id)
        replace_cleanup_claim(runtime_root, spawn_id, unresolved)


def _cleanup_after_finalize(runtime_root: Path, record: SpawnRecord) -> SpawnRecord:
    if is_terminal_spawn_status(record.status):
        _cleanup_claimed_scopes(runtime_root, record)
    return record


def _finalize_derived(
    project_root: Path,
    runtime_root: Path,
    stored: SpawnRecord,
    derived: SpawnRecord,
) -> SpawnRecord:
    terminal = derived.terminal
    if terminal is None or not is_terminal_spawn_status(derived.status):
        return stored
    managed_scopes_pending = is_managed_primary_candidate(stored)
    outcome = asyncio.run(
        build_spawn_application_service_from_roots(
            project_root, runtime_root
        ).complete_spawn(
            SpawnId(stored.id),
            derived.status,
            terminal.exit_code,
            origin="reconciler",
            error=terminal.error,
            managed_scopes_pending=managed_scopes_pending,
        )
    )
    reconciled = outcome.snapshot or stored
    if outcome.wrote:
        logger.debug(
            "Reconciled active spawn after acquiring its free launch lock.",
            spawn_id=stored.id,
            status=derived.status,
            error=terminal.error,
        )
    return reconciled


def iter_repair_candidates(runtime_root: Path) -> Iterator[SpawnRecord]:
    """Stream active rows and terminal rows with unfinished cleanup claims."""

    spawns_dir = runtime_root / "spawns"
    for spawn_id in iter_spawn_ids(spawns_dir):
        try:
            record = read_state(spawns_dir, spawn_id, include_prompt=False)
        except SpawnStateQuarantined:
            continue
        if record is None or record.record_mode == "historical":
            continue
        if (
            is_active_spawn_status(record.status)
            or cleanup_claim_path(runtime_root, spawn_id).is_file()
        ):
            yield record


def reconcile_active_spawn(
    project_root: Path,
    runtime_root: Path,
    record: SpawnRecord,
) -> SpawnRecord:
    """Finalize an abandoned active row only while owning its launch lock."""
    if record.record_mode == "historical" or not is_root_side_effect_process():
        return record
    if is_terminal_spawn_status(record.status):
        spawn_dir = runtime_root / "spawns" / record.id
        try:
            launch_handle = acquire(spawn_dir)
        except (Busy, Contended, NotFound):
            return record
        with launch_handle:
            try:
                current = read_state(spawn_dir.parent, record.id, include_prompt=False)
            except SpawnStateQuarantined:
                return record
            if current is None or current.record_mode == "historical":
                return record
            return _cleanup_after_finalize(runtime_root, current)
    if not is_active_spawn_status(record.status):
        return record

    claimed = claim_dead_launch(runtime_root, record.id)
    if claimed is None:
        return record
    launch_handle, derived = claimed

    with launch_handle:
        primary_metadata = read_primary_metadata(runtime_root, derived.id)
        _claim_reaper_cleanup(runtime_root, derived, primary_metadata)
        return _cleanup_after_finalize(
            runtime_root,
            _finalize_derived(project_root, runtime_root, record, derived),
        )
