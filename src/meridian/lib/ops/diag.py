"""Doctor operation for file-authoritative state health and repair."""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict

from meridian.lib.catalog.mars_roots import MarsRoots, mars_content_roots
from meridian.lib.config.loader import load_user_config
from meridian.lib.core.depth import is_root_side_effect_process
from meridian.lib.core.process_cleanup import reclaim_session_owned_scopes_for_chats
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.config_surface import build_config_surface
from meridian.lib.ops.mars import check_upgrade_availability, format_upgrade_availability
from meridian.lib.ops.pruning import (
    AdHocProjectDir,
    OrphanProjectDir,
    StaleSpawnArtifact,
    prune_adhoc_project_dirs,
    prune_orphan_project_dirs,
    prune_stale_spawn_artifacts,
    scan_adhoc_project_dirs,
    scan_orphan_project_dirs,
    scan_stale_spawn_artifacts,
)
from meridian.lib.ops.runtime import (
    resolve_runtime_authority_for_read,
    resolve_runtime_authority_for_write,
)
from meridian.lib.state import spawn_store, work_repository, work_store
from meridian.lib.state.lock_gc import LockGcStats, gc_orphaned_locks
from meridian.lib.state.session_fold import UnsupportedSessionFormat
from meridian.lib.state.session_store import cleanup_stale_sessions
from meridian.lib.state.spawn.dogfood_migration import migrate_dogfood_spawn_rows
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import overlay_derived
from meridian.lib.state.user_paths import get_user_home
from meridian.lib.telemetry.retention import (
    DEFAULT_MAX_TOTAL_BYTES,
    RetentionStats,
    run_retention_cleanup,
    scan_telemetry_segments,
)

logger = structlog.get_logger(__name__)


class DoctorInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None
    prune: bool = False
    global_: bool = False
    kill_orphans: bool = False


class TelemetryCleanupStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_segments: int = 0
    total_bytes: int = 0
    live_segments: int = 0
    orphaned_segments: int = 0
    expired_segments: int = 0
    deleted_segments: int = 0
    deleted_bytes: int = 0


class DoctorOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    project_root: str
    project_root_source: str
    identity_tier: Literal["committed", "adhoc"]
    project_id: str | None
    mars_roots: MarsRoots
    runtime_root: str | None
    runtime_root_source: str
    runs_checked: int
    agents_dir: str
    skills_dir: str
    orphan_project_dirs: tuple[OrphanProjectDir, ...] = ()
    adhoc_project_dirs: tuple[AdHocProjectDir, ...] = ()
    stale_spawn_artifacts: tuple[StaleSpawnArtifact, ...] = ()
    pruned_orphan_dirs: int = 0
    pruned_adhoc_dirs: int = 0
    pruned_spawn_artifacts: int = 0
    lock_gc: LockGcStats = LockGcStats()
    telemetry_cleanup: TelemetryCleanupStats | None = None
    warnings: tuple["DoctorWarning", ...] = ()
    repaired: tuple[str, ...] = ()
    legacy_pi_sessions: str | None = None
    killed_orphan_spawns: tuple[str, ...] = ()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        """Key-value health check output for text output mode."""
        from meridian.lib.core.formatting import kv_block

        status = "ok" if self.ok else "WARNINGS"
        pairs: list[tuple[str, str | None]] = [
            ("ok", status),
            ("project_root", self.project_root),
            ("project_root_source", self.project_root_source),
            ("identity_tier", self.identity_tier),
            ("project_id", self.project_id or "(unminted)"),
            ("mars_roots.primary", self.mars_roots.primary.as_posix()),
            ("mars_roots.scope", self.mars_roots.scope),
            (
                "mars_roots.user",
                self.mars_roots.user.as_posix() if self.mars_roots.user is not None else None,
            ),
            ("project_state", "available" if self.runtime_root is not None else "no state here"),
            ("runtime_root", self.runtime_root or "(unmaterialized)"),
            ("runtime_root_source", self.runtime_root_source),
            ("runs_checked", str(self.runs_checked)),
            ("agents_dir", self.agents_dir),
            ("skills_dir", self.skills_dir),
            ("orphan_project_dirs", str(len(self.orphan_project_dirs))),
            ("adhoc_project_dirs", str(len(self.adhoc_project_dirs))),
            ("stale_spawn_artifacts", str(len(self.stale_spawn_artifacts))),
            ("pruned_orphan_dirs", str(self.pruned_orphan_dirs)),
            ("pruned_adhoc_dirs", str(self.pruned_adhoc_dirs)),
            ("pruned_spawn_artifacts", str(self.pruned_spawn_artifacts)),
            ("orphan_locks_seen", str(self.lock_gc.files_seen)),
            ("orphan_locks_removed", str(self.lock_gc.files_removed)),
            ("orphan_locks_contended", str(self.lock_gc.files_contended)),
            ("killed_orphan_spawns", ", ".join(self.killed_orphan_spawns) or "none"),
            ("repaired", ", ".join(self.repaired) if self.repaired else "none"),
        ]
        if self.telemetry_cleanup is not None:
            tc = self.telemetry_cleanup
            pairs.append(("telemetry_segments", str(tc.total_segments)))
            pairs.append(("telemetry_bytes", f"{tc.total_bytes:,}"))
            if tc.deleted_segments > 0:
                pairs.append(
                    (
                        "telemetry_pruned",
                        f"{tc.deleted_segments} segments ({tc.deleted_bytes:,} bytes)",
                    )
                )
        result = kv_block(pairs)
        if self.legacy_pi_sessions:
            result += f"\nlegacy_pi_sessions: {self.legacy_pi_sessions}"
        for warning in self.warnings:
            result += f"\nwarning: {warning.code}: {warning.message}"
        for candidate in self.adhoc_project_dirs:
            result += (
                f"\nadhoc_project: {candidate.project_id}"
                f"\n  origin_path: {candidate.path or '(unknown)'}"
                f"\n  reason: {candidate.reason}"
                f"\n  last_activity: {candidate.last_activity}"
                f"\n  size_bytes: {candidate.size_bytes}"
                f"\n  prunable: {'yes' if candidate.prunable else 'no'}"
                f"\n  state: {candidate.runtime_root}"
            )
        return result


class DoctorWarning(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    payload: dict[str, object] | None = None


@dataclass(frozen=True)
class LocalReport:
    spawns: tuple[SpawnRecord, ...] = ()
    stale_spawn_artifacts: tuple[StaleSpawnArtifact, ...] = ()
    pruned_spawn_artifacts: int = 0
    lock_gc: LockGcStats = field(default_factory=LockGcStats)
    telemetry_cleanup: TelemetryCleanupStats | None = None
    warnings: tuple[DoctorWarning, ...] = ()
    repaired: tuple[str, ...] = ()
    legacy_pi_sessions: str | None = None
    killed_orphan_spawns: tuple[str, ...] = ()


@dataclass(frozen=True)
class GlobalReport:
    orphan_project_dirs: tuple[OrphanProjectDir, ...] = ()
    adhoc_project_dirs: tuple[AdHocProjectDir, ...] = ()
    pruned_orphan_dirs: int = 0
    pruned_adhoc_dirs: int = 0
    warnings: tuple[DoctorWarning, ...] = ()
    repaired: tuple[str, ...] = ()


def _telemetry_cleanup_stats(stats: RetentionStats) -> TelemetryCleanupStats:
    return TelemetryCleanupStats(
        total_segments=stats.total_segments,
        total_bytes=stats.total_bytes,
        live_segments=stats.live_segments,
        orphaned_segments=stats.orphaned_segments,
        expired_segments=stats.expired_segments,
        deleted_segments=stats.deleted_segments,
        deleted_bytes=stats.deleted_bytes,
    )


def _check_legacy_worktree_temp(runtime_root: Path) -> DoctorWarning | None:
    legacy_temp_dir = runtime_root / "worktree-temp"
    if not legacy_temp_dir.is_dir():
        return None
    return DoctorWarning(
        code="legacy_worktree_temp_dir",
        message=(
            "Legacy worktree-temp directory exists and is no longer used. Safe to delete manually."
        ),
        payload={"path": legacy_temp_dir.as_posix()},
    )


def _repair_stale_session_locks(
    project_root: Path,
    *,
    runtime_root: Path | None = None,
) -> int:
    resolved_runtime_root = runtime_root
    if resolved_runtime_root is None:
        resolved_runtime_root = resolve_runtime_authority_for_write(project_root).runtime_root
    if resolved_runtime_root is None:
        raise ValueError("Doctor lock repair requires a runtime root.")
    cleanup = cleanup_stale_sessions(resolved_runtime_root)
    reclaim_session_owned_scopes_for_chats(
        resolved_runtime_root,
        cleanup.cleaned_ids,
        grace_seconds=5.0,
        require_live_root=True,
    )
    return len(cleanup.cleaned_ids)


def _repair_orphan_runs(
    project_root: Path,
    *,
    runtime_root: Path | None = None,
) -> tuple[int, tuple[str, ...]]:
    from meridian.lib.state.reaper import iter_repair_candidates, reconcile_active_spawn

    resolved_runtime_root = runtime_root
    if resolved_runtime_root is None:
        resolved_runtime_root = resolve_runtime_authority_for_write(project_root).runtime_root
    if resolved_runtime_root is None:
        raise ValueError("Doctor orphan-run repair requires a runtime root.")
    # Terminal rows may carry a durable reaper cleanup claim left by a crash
    # after finalization. The reconciler is a no-op for other terminal rows.
    reconciled_orphans: list[str] = []
    orphan_primaries: list[str] = []
    for spawn in iter_repair_candidates(resolved_runtime_root):
        was_active = is_active_spawn_status(spawn.status)
        reconciled = reconcile_active_spawn(project_root, resolved_runtime_root, spawn)
        if not was_active or is_active_spawn_status(reconciled.status):
            continue
        reconciled_orphans.append(reconciled.id)
        if reconciled.kind == "primary":
            orphan_primaries.append(reconciled.id)
    reconciled_orphans.sort()
    # Capture only after every orphan is terminal: the capture guard defers
    # while any row bound to the same native session is still active, and
    # crashed --continue generations share one native session.
    if orphan_primaries:
        from meridian.lib.ops.session_archive import (
            NativeCaptureDeferred,
            materialize_native_history,
        )

        for spawn_id in sorted(orphan_primaries):
            try:
                materialize_native_history(project_root, resolved_runtime_root, spawn_id)
            except NativeCaptureDeferred as exc:
                logger.debug(
                    "orphan_primary_capture_deferred",
                    spawn_id=spawn_id,
                    owner=exc.owner,
                )
            except Exception:
                logger.warning(
                    "orphan_primary_capture_failed",
                    spawn_id=spawn_id,
                    exc_info=True,
                )
    return len(reconciled_orphans), tuple(reconciled_orphans)


def schedule_background_repairs(project_root: Path, *, repair_orphans: bool = True) -> None:
    """Resolve runtime root and run stale-lock + orphan-run repairs in a daemon thread.

    Callers gate on startup class and side-effect eligibility before calling.
    ``repair_orphans=False`` is for a launch that continues a spawn: its own
    sweep acquires the source's launch lock, which the orphan repair would
    otherwise contend for.
    """

    if not is_root_side_effect_process():
        return
    runtime_root = resolve_runtime_authority_for_write(project_root).runtime_root
    if runtime_root is None:
        return

    import threading

    # Eagerly import reaper before spawning the thread — its transitive chain
    # (reaper → managed_primary → connections.base) races with the main-thread
    # harness bootstrap, causing a _ModuleLock deadlock on Python 3.13+.
    import meridian.lib.state.reaper  # noqa: F401  # pyright: ignore[reportUnusedImport]  # Import triggers reaper registration.

    def _run_repairs() -> None:
        from contextlib import suppress

        # A newer build's session rows make these repairs refuse; that is expected
        # in a mixed-build runtime, so it goes to the debug log, not the user.
        with suppress(Exception):
            from meridian.lib.ops.legacy_native_import import bind_late_legacy_sessions

            try:
                bind_late_legacy_sessions(runtime_root)
            except UnsupportedSessionFormat as exc:
                logger.debug("legacy_late_bind_skipped", error=str(exc))
        with suppress(Exception):
            from meridian.lib.ops.legacy_native_import import (
                configured_archive_destination,
                recover_legacy_pi_sessions,
            )

            try:
                recover_legacy_pi_sessions(
                    runtime_root, archive_destination=configured_archive_destination(project_root)
                )
            except UnsupportedSessionFormat as exc:
                logger.debug("legacy_pi_recovery_skipped", error=str(exc))
        with suppress(Exception):
            dogfood = migrate_dogfood_spawn_rows(runtime_root)
            if dogfood.failed:
                logger.warning("dogfood_spawn_rows_failed", failed=dict(dogfood.failed))
            if dogfood.index_rearm_warning:
                logger.warning("dogfood_index_rearm_failed", warning=dogfood.index_rearm_warning)
        with suppress(Exception):
            _repair_stale_session_locks(project_root, runtime_root=runtime_root)
            gc_orphaned_locks(runtime_root)
            if repair_orphans:
                _repair_orphan_runs(project_root, runtime_root=runtime_root)

    threading.Thread(
        target=_run_repairs,
        name="meridian-background-repairs",
        daemon=True,
    ).start()


def _doctor_local(
    runtime_root: Path,
    *,
    project_root: Path,
    project_state_dir: Path,
    retention_days: int,
    now: float,
    prune: bool,
    kill_orphans: bool,
) -> LocalReport:
    from meridian.lib.ops.legacy_native_import import (
        bind_late_legacy_sessions,
        configured_archive_destination,
        recover_legacy_pi_sessions,
    )

    repaired: list[str] = []
    warnings: list[DoctorWarning] = []

    late_bindings = bind_late_legacy_sessions(runtime_root)
    if late_bindings.bound:
        repaired.append("late_legacy_bindings")
    pi_recovery = recover_legacy_pi_sessions(
        runtime_root,
        archive_destination=configured_archive_destination(project_root),
    )
    legacy_pi_summary: str | None = None
    if pi_recovery.bound:
        repaired.append("legacy_pi_sessions")
        legacy_pi_summary = (
            f"bound {pi_recovery.bound} old Pi chat(s) to their native sessions; "
            f"{len(pi_recovery.unbound)} left unbound"
        )
        if pi_recovery.unbound:
            legacy_pi_summary += (
                f"; inspect one with `meridian session repair cN` (e.g. {pi_recovery.unbound[0]})"
            )

    dogfood = migrate_dogfood_spawn_rows(runtime_root)
    if dogfood.migrated:
        repaired.append("dogfood_spawn_rows")
    if dogfood.failed:
        warnings.append(
            DoctorWarning(
                code="dogfood_spawn_rows_failed",
                message=(
                    f"{len(dogfood.failed)} dogfood spawn row(s) could not be migrated "
                    "and stay quarantined: "
                    + "; ".join(f"{spawn_id} ({reason})" for spawn_id, reason in dogfood.failed)
                ),
                payload={
                    "spawn_ids": [spawn_id for spawn_id, _ in dogfood.failed],
                    "reasons": dict(dogfood.failed),
                },
            )
        )
    if dogfood.index_rearm_warning:
        warnings.append(
            DoctorWarning(
                code="dogfood_index_rearm_failed",
                message=dogfood.index_rearm_warning,
            )
        )

    stale_locks = _repair_stale_session_locks(project_root, runtime_root=runtime_root)
    if stale_locks > 0:
        repaired.append("stale_session_locks")
    lock_gc = gc_orphaned_locks(runtime_root)

    active_work_items, _ = work_store.list_work_items(project_state_dir)
    archived_work_items, _ = work_store.list_archived_work_items(
        project_state_dir,
        all_archived=True,
    )
    healed_work_items = 0
    for work_id in {item.name for item in (*active_work_items, *archived_work_items)}:
        if work_store.work_item_needs_repair(project_state_dir, work_id):
            work_repository.repair_work_item(project_state_dir, work_id)
            healed_work_items += 1
    if healed_work_items:
        repaired.append("work_item_metadata")

    killed_orphan_spawns: tuple[str, ...] = ()
    if kill_orphans and is_root_side_effect_process():
        orphan_runs, killed_orphan_spawns = _repair_orphan_runs(
            project_root,
            runtime_root=runtime_root,
        )
        if orphan_runs > 0:
            repaired.append("orphan_runs")

    spawn_scan = spawn_store.list_spawns(runtime_root)
    spawns = spawn_scan.records
    # Destructive pruning stays conservative: stored-active rows remain protected.
    active_spawn_ids = {spawn.id for spawn in spawns if is_active_spawn_status(spawn.status)}
    protected_spawn_ids = active_spawn_ids | {
        quarantine.spawn_id for quarantine in spawn_scan.quarantines
    }
    stale_spawn_artifacts = tuple(
        scan_stale_spawn_artifacts(
            runtime_root,
            retention_days,
            protected_spawn_ids,
            now,
        )
    )

    pruned_spawn_artifacts = 0
    if prune:
        spawn_prune = prune_stale_spawn_artifacts(list(stale_spawn_artifacts))
        pruned_spawn_artifacts = spawn_prune.removed
        lock_gc = lock_gc.combine(spawn_prune.lock_gc)
        if pruned_spawn_artifacts > 0:
            repaired.append("spawn_artifacts")
    if lock_gc.files_removed > 0:
        repaired.append("orphaned_locks")

    telemetry_dir = runtime_root / "telemetry"
    telemetry_retention = (
        run_retention_cleanup(
            telemetry_dir,
            runtime_root=runtime_root,
            max_age_days=retention_days,
        )
        if prune
        else scan_telemetry_segments(
            telemetry_dir,
            runtime_root=runtime_root,
            max_age_days=retention_days,
        )
    )
    telemetry_cleanup = _telemetry_cleanup_stats(telemetry_retention)
    if telemetry_cleanup.deleted_segments > 0:
        repaired.append("telemetry_segments")

    legacy_worktree_temp_warning = _check_legacy_worktree_temp(runtime_root)
    if legacy_worktree_temp_warning is not None:
        warnings.append(legacy_worktree_temp_warning)
    if not prune and stale_spawn_artifacts:
        warnings.append(
            DoctorWarning(
                code="stale_spawn_artifacts",
                message=(
                    f"{len(stale_spawn_artifacts)} stale spawn artifact dir(s) would be "
                    "pruned with --prune."
                ),
                payload={
                    "spawn_ids": [item.spawn_id for item in stale_spawn_artifacts],
                    "paths": [item.path for item in stale_spawn_artifacts],
                },
            )
        )
    if not prune and (
        telemetry_cleanup.expired_segments > 0
        or telemetry_cleanup.total_bytes > DEFAULT_MAX_TOTAL_BYTES
    ):
        reasons: list[str] = []
        if telemetry_cleanup.expired_segments > 0:
            reasons.append(f"{telemetry_cleanup.expired_segments} expired segment(s)")
        if telemetry_cleanup.total_bytes > DEFAULT_MAX_TOTAL_BYTES:
            reasons.append(
                f"{telemetry_cleanup.total_bytes:,} bytes exceeds "
                f"{DEFAULT_MAX_TOTAL_BYTES:,} byte cap"
            )
        warnings.append(
            DoctorWarning(
                code="stale_telemetry_segments",
                message=(
                    "Telemetry retention cleanup would prune local segment(s) with "
                    f"--prune: {'; '.join(reasons)}."
                ),
                payload={
                    "total_segments": telemetry_cleanup.total_segments,
                    "total_bytes": telemetry_cleanup.total_bytes,
                    "expired_segments": telemetry_cleanup.expired_segments,
                    "max_total_bytes": DEFAULT_MAX_TOTAL_BYTES,
                },
            )
        )

    derived = overlay_derived(runtime_root, spawns)
    running = [row.id for row in derived if is_active_spawn_status(row.status)]
    # Stored-active but derived-terminal: the runner is gone and no outcome is
    # persisted yet. Readers already show the crash; --kill-orphans writes it.
    unpersisted = [
        row.id
        for row, stored in zip(derived, spawns, strict=True)
        if is_active_spawn_status(stored.status) and not is_active_spawn_status(row.status)
    ]
    if unpersisted:
        warnings.append(
            DoctorWarning(
                code="dead_spawns_unfinalized",
                message=(
                    "Spawns whose runner is gone have no persisted outcome; "
                    "`meridian doctor --kill-orphans` records it and reaps their processes: "
                    + ", ".join(unpersisted)
                ),
                payload={"spawn_ids": unpersisted},
            )
        )
    if running:
        reconciliation_mode = (
            "after orphan-kill reconciliation"
            if kill_orphans
            else "without orphan-kill reconciliation"
        )
        warnings.append(
            DoctorWarning(
                code="live_active_spawns_remain",
                message=(
                    f"Live active spawns remain {reconciliation_mode} and were not pruned: "
                    + ", ".join(running)
                ),
                payload={"spawn_ids": running},
            )
        )

    return LocalReport(
        spawns=spawns,
        stale_spawn_artifacts=stale_spawn_artifacts,
        pruned_spawn_artifacts=pruned_spawn_artifacts,
        lock_gc=lock_gc,
        telemetry_cleanup=telemetry_cleanup,
        warnings=tuple(warnings),
        repaired=tuple(repaired),
        legacy_pi_sessions=legacy_pi_summary,
        killed_orphan_spawns=killed_orphan_spawns,
    )


def _doctor_global(
    now: float,
    *,
    retention_days: int,
    prune: bool,
) -> GlobalReport:
    user_home = get_user_home()
    orphan_project_dirs = tuple(scan_orphan_project_dirs(user_home, retention_days, now))
    adhoc_retention_days = load_user_config().state.adhoc_retention_days
    adhoc_project_dirs = tuple(scan_adhoc_project_dirs(user_home, adhoc_retention_days, now))
    prunable_adhoc_project_dirs = tuple(
        candidate for candidate in adhoc_project_dirs if candidate.prunable
    )
    warnings: list[DoctorWarning] = []
    repaired: list[str] = []
    pruned_orphan_dirs = 0
    pruned_adhoc_dirs = 0

    if prune:
        pruned_orphan_dirs = prune_orphan_project_dirs(list(orphan_project_dirs))
        pruned_adhoc_dirs = prune_adhoc_project_dirs(
            list(adhoc_project_dirs),
            retention_days=adhoc_retention_days,
            now=now,
        )
        if pruned_orphan_dirs > 0:
            repaired.append("orphan_project_dirs")
        if pruned_adhoc_dirs > 0:
            repaired.append("adhoc_project_dirs")
    else:
        if prunable_adhoc_project_dirs:
            warnings.append(
                DoctorWarning(
                    code="stale_adhoc_project_dirs",
                    message=(
                        f"{len(prunable_adhoc_project_dirs)} ad-hoc project dir(s) would be pruned "
                        "with --prune --global."
                    ),
                    payload={
                        "project_ids": [item.project_id for item in prunable_adhoc_project_dirs],
                        "paths": [
                            item.path or item.runtime_root for item in prunable_adhoc_project_dirs
                        ],
                        "reasons": [item.reason for item in prunable_adhoc_project_dirs],
                    },
                )
            )
        if orphan_project_dirs:
            warnings.append(
                DoctorWarning(
                    code="stale_orphan_project_dirs",
                    message=(
                        f"{len(orphan_project_dirs)} stale project dir(s) would be pruned "
                        "with --prune --global."
                    ),
                    payload={
                        "project_uuids": [item.uuid for item in orphan_project_dirs],
                        "paths": [item.path for item in orphan_project_dirs],
                    },
                )
            )

    return GlobalReport(
        orphan_project_dirs=orphan_project_dirs,
        adhoc_project_dirs=adhoc_project_dirs,
        pruned_orphan_dirs=pruned_orphan_dirs,
        pruned_adhoc_dirs=pruned_adhoc_dirs,
        warnings=tuple(warnings),
        repaired=tuple(repaired),
    )


def doctor_sync(payload: DoctorInput) -> DoctorOutput:
    authority = resolve_runtime_authority_for_read(payload.project_root)
    surface = build_config_surface(authority)
    project_root = surface.project_root
    runtime_root = (
        authority.runtime_root
        if authority.runtime_root is not None and authority.runtime_root.is_dir()
        else None
    )
    retention_days = surface.resolved_config.state.retention_days
    now = time.time()
    if payload.global_ and not is_root_side_effect_process():
        raise RuntimeError(
            "Global doctor maintenance requires a root Meridian process. "
            "Run 'meridian doctor --global' from a top-level shell, not from a nested spawn."
        )

    local_report = (
        _doctor_local(
            runtime_root,
            project_root=project_root,
            project_state_dir=authority.project_state_dir,
            retention_days=retention_days,
            now=now,
            prune=payload.prune,
            kill_orphans=payload.kill_orphans,
        )
        if runtime_root is not None
        else LocalReport()
    )
    # Global root deletion is last: local prune/telemetry helpers may create
    # coordination directories inside the current runtime root.
    global_report = (
        _doctor_global(now, retention_days=retention_days, prune=payload.prune)
        if payload.global_
        else GlobalReport()
    )

    mars_roots = authority.mars_roots
    agents_dir = mars_roots.primary / ".mars" / "agents"
    skills_dir = mars_roots.primary / ".mars" / "skills"
    agents_dirs = [
        root / ".mars" / "agents"
        for root in mars_content_roots(mars_roots)
        if (root / ".mars" / "agents").is_dir()
    ]
    skills_dirs = [
        root / ".mars" / "skills"
        for root in mars_content_roots(mars_roots)
        if (root / ".mars" / "skills").is_dir()
    ]

    warnings = [*local_report.warnings, *global_report.warnings]
    if surface.warning is not None:
        warnings.append(
            DoctorWarning(
                code="missing_project_root",
                message=surface.warning,
            )
        )
    for finding in surface.workspace_findings:
        warnings.append(
            DoctorWarning(
                code=finding.code,
                message=finding.message,
                payload=finding.payload,
            )
        )
    mars_marker_exists = (mars_roots.primary / "mars.toml").is_file()
    if mars_marker_exists and not skills_dirs:
        warnings.append(
            DoctorWarning(
                code="missing_skills_directories",
                message="No configured skills directories were found.",
            )
        )
    if mars_marker_exists and not agents_dirs:
        warnings.append(
            DoctorWarning(
                code="missing_agent_profile_directories",
                message="No configured agent profile directories were found.",
            )
        )
    if mars_marker_exists:
        availability = check_upgrade_availability(project_root)
        if availability is None:
            warnings.append(
                DoctorWarning(
                    code="updates_check_failed",
                    message=(
                        "Could not check for dependency updates "
                        "(`meridian mars outdated --json` failed)."
                    ),
                )
            )
        elif availability.count > 0:
            warning_lines = format_upgrade_availability(availability, style="warning")
            warnings.append(
                DoctorWarning(
                    code="outdated_dependencies",
                    message="\n".join(warning_lines),
                    payload={
                        "within_constraint": list(availability.within_constraint),
                        "beyond_constraint": list(availability.beyond_constraint),
                    },
                )
            )

    repaired = tuple(sorted({*local_report.repaired, *global_report.repaired}))
    return DoctorOutput(
        ok=not warnings,
        project_root=project_root.as_posix(),
        project_root_source=surface.authority.project_root_source,
        identity_tier=authority.identity_tier,
        project_id=authority.project_id,
        mars_roots=authority.mars_roots,
        runtime_root=runtime_root.as_posix() if runtime_root is not None else None,
        runtime_root_source=authority.runtime_root_source or "unresolved",
        runs_checked=len(local_report.spawns),
        agents_dir=agents_dir.as_posix(),
        skills_dir=skills_dir.as_posix(),
        orphan_project_dirs=global_report.orphan_project_dirs,
        adhoc_project_dirs=global_report.adhoc_project_dirs,
        stale_spawn_artifacts=local_report.stale_spawn_artifacts,
        pruned_orphan_dirs=global_report.pruned_orphan_dirs,
        pruned_adhoc_dirs=global_report.pruned_adhoc_dirs,
        pruned_spawn_artifacts=local_report.pruned_spawn_artifacts,
        lock_gc=local_report.lock_gc,
        telemetry_cleanup=local_report.telemetry_cleanup,
        warnings=tuple(warnings),
        repaired=repaired,
        legacy_pi_sessions=local_report.legacy_pi_sessions,
        killed_orphan_spawns=local_report.killed_orphan_spawns,
    )


async def doctor(payload: DoctorInput) -> DoctorOutput:
    return await asyncio.to_thread(doctor_sync, payload)
