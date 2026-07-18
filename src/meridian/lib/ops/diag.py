"""Doctor operation for file-authoritative state health and repair."""

import asyncio
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.depth import is_root_side_effect_process
from meridian.lib.core.process_cleanup import reclaim_session_owned_scopes_for_chat
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.config import ensure_runtime_state_bootstrap_sync
from meridian.lib.ops.config_surface import build_config_surface
from meridian.lib.ops.mars import check_upgrade_availability, format_upgrade_availability
from meridian.lib.ops.pruning import (
    OrphanProjectDir,
    StaleSpawnArtifact,
    prune_orphan_project_dirs,
    prune_stale_spawn_artifacts,
    scan_orphan_project_dirs,
    scan_stale_spawn_artifacts,
)
from meridian.lib.ops.runtime import resolve_project_authority, resolve_runtime_authority_for_write
from meridian.lib.state import spawn_store, work_repository, work_store
from meridian.lib.state.lock_gc import LockGcStats, gc_orphaned_locks
from meridian.lib.state.session_store import cleanup_stale_sessions
from meridian.lib.state.user_paths import get_user_home
from meridian.lib.telemetry.retention import (
    DEFAULT_MAX_TOTAL_BYTES,
    RetentionStats,
    run_retention_cleanup,
    scan_telemetry_segments,
)


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
    runtime_root: str
    runtime_root_source: str
    runs_checked: int
    agents_dir: str
    skills_dir: str
    orphan_project_dirs: tuple[OrphanProjectDir, ...] = ()
    stale_spawn_artifacts: tuple[StaleSpawnArtifact, ...] = ()
    pruned_orphan_dirs: int = 0
    pruned_spawn_artifacts: int = 0
    lock_gc: LockGcStats = LockGcStats()
    telemetry_cleanup: TelemetryCleanupStats | None = None
    warnings: tuple["DoctorWarning", ...] = ()
    repaired: tuple[str, ...] = ()
    killed_orphan_spawns: tuple[str, ...] = ()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        """Key-value health check output for text output mode."""
        from meridian.lib.core.formatting import kv_block

        status = "ok" if self.ok else "WARNINGS"
        pairs: list[tuple[str, str | None]] = [
            ("ok", status),
            ("project_root", self.project_root),
            ("project_root_source", self.project_root_source),
            ("runtime_root", self.runtime_root),
            ("runtime_root_source", self.runtime_root_source),
            ("runs_checked", str(self.runs_checked)),
            ("agents_dir", self.agents_dir),
            ("skills_dir", self.skills_dir),
            ("orphan_project_dirs", str(len(self.orphan_project_dirs))),
            ("stale_spawn_artifacts", str(len(self.stale_spawn_artifacts))),
            ("pruned_orphan_dirs", str(self.pruned_orphan_dirs)),
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
        for warning in self.warnings:
            result += f"\nwarning: {warning.code}: {warning.message}"
        return result


class DoctorWarning(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    payload: dict[str, object] | None = None


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
    for chat_id in cleanup.cleaned_ids:
        reclaim_session_owned_scopes_for_chat(
            resolved_runtime_root,
            chat_id,
            grace_seconds=5.0,
            require_live_root=True,
        )
    return len(cleanup.cleaned_ids)


def _repair_orphan_runs(
    project_root: Path,
    *,
    runtime_root: Path | None = None,
) -> tuple[int, tuple[str, ...]]:
    from meridian.lib.state.reaper import reconcile_active_spawn

    resolved_runtime_root = runtime_root
    if resolved_runtime_root is None:
        resolved_runtime_root = resolve_runtime_authority_for_write(project_root).runtime_root
    if resolved_runtime_root is None:
        raise ValueError("Doctor orphan-run repair requires a runtime root.")
    spawns = spawn_store.list_spawns(resolved_runtime_root)
    running_before = {s.id for s in spawns if is_active_spawn_status(s.status)}
    # Terminal rows may carry a durable reaper cleanup claim left by a crash
    # after finalization. The reconciler is a no-op for other terminal rows.
    reconciled = [
        reconcile_active_spawn(project_root, resolved_runtime_root, spawn) for spawn in spawns
    ]
    running_after = {s.id for s in reconciled if is_active_spawn_status(s.status)}
    reconciled_orphans = tuple(sorted(running_before - running_after))
    return len(reconciled_orphans), reconciled_orphans


def schedule_background_repairs(project_root: Path) -> None:
    """Resolve runtime root and run stale-lock + orphan-run repairs in a daemon thread.

    Callers gate on startup class and side-effect eligibility before calling.
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
    import meridian.lib.state.reaper  # noqa: F401  # pyright: ignore[reportUnusedImport]

    def _run_repairs() -> None:
        from contextlib import suppress

        with suppress(Exception):
            _repair_stale_session_locks(project_root, runtime_root=runtime_root)
            gc_orphaned_locks(runtime_root)
            _repair_orphan_runs(project_root, runtime_root=runtime_root)

    threading.Thread(
        target=_run_repairs,
        name="meridian-background-repairs",
        daemon=True,
    ).start()


def doctor_sync(payload: DoctorInput) -> DoctorOutput:
    authority = resolve_project_authority(payload.project_root)
    surface = build_config_surface(authority)
    project_root = surface.project_root
    ensure_runtime_state_bootstrap_sync(project_root)
    runtime_authority = resolve_runtime_authority_for_write(project_root)
    runtime_root = runtime_authority.runtime_root
    if runtime_root is None:
        raise ValueError("Doctor runtime authority did not resolve a runtime root.")
    retention_days = surface.resolved_config.state.retention_days
    now = time.time()

    repaired: list[str] = []
    killed_orphan_spawns: tuple[str, ...] = ()
    stale_locks = _repair_stale_session_locks(project_root)
    if stale_locks > 0:
        repaired.append("stale_session_locks")
    lock_gc = gc_orphaned_locks(runtime_root)

    project_state_dir = runtime_authority.project_state_dir
    active_work_items, _ = work_store.list_work_items(project_state_dir)
    archived_work_items, _ = work_store.list_archived_work_items(
        project_state_dir, all_archived=True
    )
    healed_work_items = 0
    for work_id in {item.name for item in (*active_work_items, *archived_work_items)}:
        if work_store.work_item_needs_repair(project_state_dir, work_id):
            work_repository.repair_work_item(project_state_dir, work_id)
            healed_work_items += 1
    if healed_work_items:
        repaired.append("work_item_metadata")

    if payload.kill_orphans and is_root_side_effect_process():
        orphan_runs, killed_orphan_spawns = _repair_orphan_runs(project_root)
        if orphan_runs > 0:
            repaired.append("orphan_runs")

    spawns = spawn_store.list_spawns(runtime_root)
    active_spawn_ids = {spawn.id for spawn in spawns if is_active_spawn_status(spawn.status)}
    orphan_project_dirs: list[OrphanProjectDir] = []
    if payload.global_:
        if not is_root_side_effect_process():
            raise RuntimeError(
                "Global doctor maintenance requires a root Meridian process. "
                "Run 'meridian doctor --global' from a top-level shell, not from a nested spawn."
            )
        orphan_project_dirs = scan_orphan_project_dirs(get_user_home(), retention_days, now)
    stale_spawn_artifacts = scan_stale_spawn_artifacts(
        runtime_root,
        retention_days,
        active_spawn_ids,
        now,
    )

    pruned_orphan_dirs = 0
    pruned_spawn_artifacts = 0
    if payload.prune:
        pruned_orphan_dirs = prune_orphan_project_dirs(orphan_project_dirs)
        spawn_prune = prune_stale_spawn_artifacts(stale_spawn_artifacts)
        pruned_spawn_artifacts = spawn_prune.removed
        lock_gc = lock_gc.combine(spawn_prune.lock_gc)
        if pruned_orphan_dirs > 0:
            repaired.append("orphan_project_dirs")
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
        if payload.prune
        else scan_telemetry_segments(
            telemetry_dir,
            runtime_root=runtime_root,
            max_age_days=retention_days,
        )
    )
    telemetry_cleanup = _telemetry_cleanup_stats(telemetry_retention)
    if telemetry_cleanup.deleted_segments > 0:
        repaired.append("telemetry_segments")

    agents_dir = project_root / ".mars" / "agents"
    skills_dir = project_root / ".mars" / "skills"
    agents_dirs = [agents_dir] if agents_dir.is_dir() else []
    skills_dirs = [skills_dir] if skills_dir.is_dir() else []

    warnings: list[DoctorWarning] = []
    legacy_worktree_temp_warning = _check_legacy_worktree_temp(runtime_root)
    if legacy_worktree_temp_warning is not None:
        warnings.append(legacy_worktree_temp_warning)
    if surface.warning is not None:
        warnings.append(
            DoctorWarning(
                code="missing_project_root",
                message=surface.warning,
            )
        )
    if not payload.prune and orphan_project_dirs:
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
    if not payload.prune and stale_spawn_artifacts:
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
    if not payload.prune and (
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
    for finding in surface.workspace_findings:
        warnings.append(
            DoctorWarning(
                code=finding.code,
                message=finding.message,
                payload=finding.payload,
            )
        )
    if not skills_dirs:
        warnings.append(
            DoctorWarning(
                code="missing_skills_directories",
                message="No configured skills directories were found.",
            )
        )
    if not agents_dirs:
        warnings.append(
            DoctorWarning(
                code="missing_agent_profile_directories",
                message="No configured agent profile directories were found.",
            )
        )

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

    running = [row.id for row in spawns if is_active_spawn_status(row.status)]
    if running:
        reconciliation_mode = (
            "after orphan-kill reconciliation"
            if payload.kill_orphans
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

    return DoctorOutput(
        ok=not warnings,
        project_root=project_root.as_posix(),
        project_root_source=surface.authority.project_root_source,
        runtime_root=runtime_root.as_posix(),
        runtime_root_source=runtime_authority.runtime_root_source or "unresolved",
        runs_checked=len(spawns),
        agents_dir=agents_dir.as_posix(),
        skills_dir=skills_dir.as_posix(),
        orphan_project_dirs=tuple(orphan_project_dirs),
        stale_spawn_artifacts=tuple(stale_spawn_artifacts),
        pruned_orphan_dirs=pruned_orphan_dirs,
        pruned_spawn_artifacts=pruned_spawn_artifacts,
        lock_gc=lock_gc,
        telemetry_cleanup=telemetry_cleanup,
        warnings=tuple(warnings),
        repaired=tuple(sorted(set(repaired))),
        killed_orphan_spawns=killed_orphan_spawns,
    )


async def doctor(payload: DoctorInput) -> DoctorOutput:
    return await asyncio.to_thread(doctor_sync, payload)
