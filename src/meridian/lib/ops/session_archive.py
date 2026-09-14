"""Finite history retention and inert restore policy; files decide reclamation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.config.settings import HistoryArchiveConfig, load_config
from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.types import SpawnId
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.history_codec import last_activity
from meridian.lib.state.history_index import HistoryIndex, HistorySnapshot, transcript_activity
from meridian.lib.state.process_scope_projection import read_scope_projection
from meridian.lib.state.reaper import scope_liveness
from meridian.lib.state.retention_archive import (
    ArchivedRecord,
    append_receipt,
    archive_locations,
    archive_path,
    capture_record,
    publish_archive,
    read_receipts,
    recover_archives,
    verify_archive,
)
from meridian.lib.state.session_identity import session_records_for_spawns
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import read_state, write_state_locked


class SessionArchiveInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    project_root: str | None = None
    refs: tuple[str, ...] = ()
    destination: str | None = None
    eligible: bool = False
    list_archives: bool = False
    apply: bool = False
    after_days: int | None = Field(default=None, ge=0)


class SessionRestoreInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    project_root: str | None = None
    archive: str
    refs: tuple[str, ...]


class SessionImportInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    project_root: str | None = None
    archive: str


class RestoredHistory(BaseModel):
    model_config = ConfigDict(frozen=True)
    history_id: str
    spawn_id: str
    chat_id: str | None


class SessionArchiveOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    selected: tuple[str, ...] = ()
    protected: tuple[str, ...] = ()
    reclaimed: tuple[str, ...] = ()
    archives: tuple[str, ...] = ()
    restored: tuple[str, ...] = ()
    restored_histories: tuple[RestoredHistory, ...] = ()
    errors: tuple[str, ...] = ()
    preparation_required: tuple[str, ...] = ()
    limited: bool = False
    snapshots: tuple[HistorySnapshot, ...] = ()

    def format_text(self, ctx: object = None) -> str:
        lines = [
            f"Selected: {len(self.selected)}; reclaimed: {len(self.reclaimed)}; "
            f"restored: {len(self.restored)}; protected: {len(self.protected)}"
        ]
        lines.extend(f"Selected history: {key}" for key in self.selected)
        lines.extend(f"Archive: {path}" for path in self.archives)
        lines.extend(
            f"Restored history: {row.history_id} -> {row.spawn_id} / "
            f"{row.chat_id or 'no chat'} [historical]"
            for row in self.restored_histories
        )
        lines.extend(
            f"Requires native capture (--apply): {key}" for key in self.preparation_required
        )
        if self.limited:
            lines.append("Pass reached the configured bundle limit; repeat for remaining records.")
        for snapshot in self.snapshots:
            state = "current" if snapshot.current else "snapshot only"
            lines.append(f"History {snapshot.history_id} [{state}] {snapshot.portable_digest}")
            lines.append(f"  ZIP {snapshot.archive_id}: {snapshot.path}")
        lines.extend(f"Protected: {key}" for key in self.protected)
        lines.extend(f"Error: {error}" for error in self.errors)
        return "\n".join(lines)


def _protected(root: Path) -> tuple[dict[str, SpawnRecord], set[str]]:
    scan = spawn_store.list_spawns(root)
    if scan.quarantines:
        raise ValueError("Cannot establish retention safety while spawn records are quarantined")
    records = {record.id: record for record in scan.records}
    sessions = session_store.list_all_session_records(root)
    active_chats = {
        record.chat_id
        for record in sessions
        if record.record_mode != "historical"
        and (
            record.stopped_at is None
            or session_store.is_session_lease_owner_alive(root, record.chat_id)
        )
    }
    by_history = {row.history_id: row.id for row in records.values() if row.history_id}
    protected: set[str] = set()
    for record in records.values():
        if any(dependency not in by_history for dependency in record.retained_history_ids):
            protected.add(record.id)
        if record.record_mode == "historical":
            continue
        scopes = read_scope_projection(root, SpawnId(record.id))
        if (
            record.status not in TERMINAL_SPAWN_STATUSES
            or record.chat_id in active_chats
            or record.owner_chat_id in active_chats
            or any(
                scope_liveness(scope)["likely_serving"]
                for scope in scopes.scopes
                if scope.release_id not in scopes.released_ids
            )
        ):
            protected.add(record.id)
    # All immutable dependency edges participate, not just local parent aliases.
    # Unknown targets and cycles are unsafe selection inputs, even when terminal.
    edges: dict[str, set[str]] = {}
    for record in records.values():
        targets: set[str] = set()
        for dependency in (
            record.parent_history_id,
            record.owner_history_id,
            record.forked_from_history_id,
            *record.retained_history_ids,
        ):
            if dependency is None:
                continue
            target = by_history.get(dependency)
            if target is None:
                protected.add(record.id)
            else:
                targets.add(target)
        if record.parent_id:
            if record.parent_id in records:
                targets.add(record.parent_id)
            else:
                protected.add(record.id)
        edges[record.id] = targets
    visited: set[str] = set()
    for key in records:
        if key in visited:
            continue
        path: set[str] = set()
        stack = [(key, False)]
        while stack:
            current, exiting = stack.pop()
            if exiting:
                path.discard(current)
                visited.add(current)
            elif current in path:
                protected.update(path)
            elif current not in visited:
                path.add(current)
                stack.append((current, True))
                stack.extend((target, False) for target in edges[current])
    # Unsafe ancestry invalidates every dependent, not only the immediate owner.
    unsafe = set(protected)
    changed = True
    while changed:
        before = len(unsafe)
        unsafe.update(key for key, targets in edges.items() if targets & unsafe)
        changed = len(unsafe) != before
    protected.update(unsafe)
    changed = True
    while changed:
        before = len(protected)
        chats = {records[key].owner_chat_id or records[key].chat_id for key in protected}
        fork_chats = {
            record.forked_from_chat_id
            for record in sessions
            if record.chat_id in chats and record.forked_from_chat_id
        }
        for record in records.values():
            if record.id in protected:
                for history_id in (
                    record.parent_history_id,
                    record.owner_history_id,
                    record.forked_from_history_id,
                    *record.retained_history_ids,
                ):
                    if history_id is not None and history_id in by_history:
                        protected.add(by_history[history_id])
                if record.parent_id in records:
                    protected.add(record.parent_id)
            if record.chat_id in chats | fork_chats:
                protected.add(record.id)
        changed = len(protected) != before
    return records, protected


def archive_history(
    root: Path,
    *,
    destination: Path,
    refs: tuple[str, ...] = (),
    eligible: bool = False,
    apply: bool = False,
    after_days: int | None = None,
    policy: HistoryArchiveConfig | None = None,
    project_root: Path | None = None,
) -> SessionArchiveOutput:
    policy = policy or HistoryArchiveConfig()
    if not refs and not eligible:
        raise ValueError("Select session references or --eligible")
    after_days = policy.after_days if after_days is None else after_days
    if after_days < 0:
        raise ValueError("after_days must be nonnegative")
    destination = destination.expanduser().resolve()
    if destination == root.resolve() or root.resolve() in destination.parents:
        raise ValueError("Archive destination must be outside the runtime root")
    changes = HistoryChanges(root)
    with lock_file(root / "history-archives/archive.lock"):
        recovery_errors = recover_archives(root, destination) if apply else ()
        # No omission-sensitive policy may start from an incomplete candidate set.
        candidates = HistoryIndex(root).spawns(oldest_first=True)
        with lock_file(changes.mutation_lock, mode="shared"):
            _, protected = _protected(root)
            sessions = session_records_for_spawns(root, candidates)
        selected: list[ArchivedRecord] = []
        errors: list[str] = list(recovery_errors)
        preparation_required: list[str] = []
        limited = False
        selected_bytes = 0
        preparation_attempts = 0
        matched = {
            ref
            for ref in refs
            for row in candidates
            if ref in {row.id, str(row.history_id), row.chat_id, row.owner_chat_id}
        }
        for receipt in read_receipts(root):
            if receipt.event in {"reclaim_prepared", "reclaimed", "imported"}:
                matched.update(
                    ref
                    for ref in refs
                    for row in receipt.records
                    if ref
                    in {
                        row.state.id,
                        str(row.history_id),
                        row.state.chat_id,
                        row.state.owner_chat_id,
                    }
                )
        if refs and set(refs) - matched:
            raise ValueError(f"History references not found: {sorted(set(refs) - matched)}")
        cutoff = datetime.now(UTC) - timedelta(days=after_days)
        for candidate in candidates:
            if refs and not set(refs) & {
                candidate.id,
                str(candidate.history_id),
                candidate.chat_id,
                candidate.owner_chat_id,
            }:
                continue
            if candidate.id in protected or candidate.status not in TERMINAL_SPAWN_STATUSES:
                continue
            if len(selected) + len(preparation_required) >= policy.max_records:
                limited = True
                break
            path = root / "spawns" / candidate.id / "history.jsonl"
            if not path.exists():
                preliminary = last_activity(candidate, sessions.get(candidate.id), "")
                if eligible and datetime.fromisoformat(preliminary) > cutoff:
                    continue
                if not apply:
                    preparation_required.append(str(candidate.history_id or candidate.id))
                    continue
                if project_root is None:
                    errors.append(f"{candidate.id}: native capture requires project context")
                    continue
                if preparation_attempts >= policy.max_records:
                    limited = True
                    break
                preparation_attempts += 1
                try:
                    materialize_native_history(project_root, root, candidate.id)
                except (ValueError, OSError) as exc:
                    errors.append(f"{candidate.id}: {exc}")
                    continue
            if candidate.history_id is None and apply:
                write_state_locked(
                    root / "spawns", candidate.id, lambda row: row, allow_terminal_overwrite=True
                )
            source = HistorySource(kind="spawn", key=candidate.id)
            with lock_file(changes.mutation_lock, mode="shared"), lock_file(source.lock_path(root)):
                state = read_state(root / "spawns", candidate.id, include_prompt=False)
                if state is None:
                    continue
                try:
                    activity = last_activity(
                        state, sessions.get(state.id), transcript_activity(path, "")
                    )
                    if eligible and datetime.fromisoformat(activity) > cutoff:
                        continue
                    record = capture_record(
                        root / "spawns" / state.id, state, sessions.get(state.id), activity
                    )
                except (ValueError, OSError) as exc:
                    errors.append(f"{state.id}: {exc}")
                    continue
                size = sum(member.size for member in record.files)
                if selected and selected_bytes + size > policy.max_uncompressed_bytes:
                    limited = True
                    break
                selected.append(record)
                selected_bytes += size
        if not apply or not selected:
            return SessionArchiveOutput(
                selected=tuple(str(row.history_id) for row in selected),
                protected=tuple(sorted(protected)),
                errors=tuple(errors),
                preparation_required=tuple(preparation_required),
                limited=limited,
            )
        receipt = publish_archive(root, destination, tuple(selected))
        archive = archive_path(receipt)
        verify_archive(archive, tuple(selected))
        reclaimed: list[str] = []
        for captured in selected:
            with lock_file(changes.mutation_lock):
                records, protected_now = _protected(root)
                current = records.get(captured.state.id)
                if current is None or current.id in protected_now:
                    continue
                source = HistorySource(kind="spawn", key=current.id)
                with lock_file(source.lock_path(root)):
                    fresh = capture_record(
                        root / "spawns" / current.id,
                        current,
                        session_records_for_spawns(root, (current,)).get(current.id),
                        captured.activity,
                    )
                    if fresh.capture_fingerprint != captured.capture_fingerprint:
                        errors.append(f"{current.id}: source changed; retained loose copy")
                        continue
                    # Publish current-location receipt BEFORE removal. A crash leaves
                    # both copies, and projection always prefers the loose authority.
                    append_receipt(
                        root,
                        receipt.model_copy(
                            update={
                                "event": "reclaim_prepared",
                                "records": (captured,),
                            }
                        ),
                    )
                    if spawn_store.delete_published_spawn(
                        root,
                        current.id,
                        can_delete=lambda row, captured=captured: (
                            row is not None and row.history_id == captured.history_id
                        ),
                    ):
                        append_receipt(
                            root,
                            receipt.model_copy(
                                update={
                                    "event": "reclaimed",
                                    "records": (captured,),
                                }
                            ),
                        )
                        reclaimed.append(str(captured.history_id))
        HistoryIndex(root).catch_up()
        return SessionArchiveOutput(
            selected=tuple(str(row.history_id) for row in selected),
            protected=tuple(sorted(protected)),
            reclaimed=tuple(reclaimed),
            archives=(str(archive),),
            errors=tuple(errors),
            limited=limited,
        )


def session_archive_sync(payload: SessionArchiveInput) -> SessionArchiveOutput:
    roots = resolve_roots_for_read(payload.project_root)
    if roots is None:
        raise ValueError("No project history")
    if payload.list_archives:
        configured = (
            payload.destination or load_config(roots.project_root).history.archive.destination
        )
        if payload.refs or payload.apply or payload.eligible:
            raise ValueError("--list cannot be combined with archive selection or --apply")
        return SessionArchiveOutput(
            snapshots=HistoryIndex(roots.runtime_root).snapshots(
                destination=Path(configured).expanduser() if configured else None
            )
        )
    config = load_config(roots.project_root).history.archive
    destination = payload.destination or config.destination
    if not destination:
        raise ValueError("Set history.archive.destination or pass --destination")
    return archive_history(
        roots.runtime_root,
        destination=Path(destination),
        refs=payload.refs,
        eligible=payload.eligible,
        apply=payload.apply,
        after_days=payload.after_days,
        policy=config,
        project_root=roots.project_root,
    )


def session_restore_sync(payload: SessionRestoreInput) -> SessionArchiveOutput:
    from meridian.lib.state.retention_restore import restore_archive

    roots = resolve_roots_for_read(payload.project_root)
    if roots is None:
        raise ValueError("Initialize the destination project before restoring history")
    from meridian.lib.state.retention_archive import read_receipts

    archive = Path(payload.archive).expanduser()
    if not archive.is_file():
        receipts = tuple(
            row
            for row in reversed(read_receipts(roots.runtime_root))
            if str(row.archive_id) == payload.archive
        )
        if not receipts:
            raise ValueError("Archive path or identity not found")
        configured = load_config(roots.project_root).history.archive.destination
        archive = archive_locations(
            receipts, destination=Path(configured).expanduser() if configured else None, full=True
        )[0].path
    restored = restore_archive(roots.runtime_root, archive, payload.refs)
    HistoryIndex(roots.runtime_root).catch_up()
    mappings = []
    for key in restored:
        row = read_state(roots.runtime_root / "spawns", key, include_prompt=False)
        if row is not None:
            mappings.append(
                RestoredHistory(history_id=str(row.history_id), spawn_id=key, chat_id=row.chat_id)
            )
    return SessionArchiveOutput(restored=restored, restored_histories=tuple(mappings))


def session_import_sync(payload: SessionImportInput) -> SessionArchiveOutput:
    from meridian.lib.state.retention_archive import import_archive

    roots = resolve_roots_for_read(payload.project_root)
    if roots is None:
        raise ValueError("Initialize the destination project before importing history")
    receipt = import_archive(roots.runtime_root, Path(payload.archive))
    HistoryIndex(roots.runtime_root).catch_up()
    return SessionArchiveOutput(
        selected=tuple(str(row.history_id) for row in receipt.records),
        archives=(str(Path(receipt.destination) / receipt.zip_name),),
    )


session_import = async_from_sync(session_import_sync)

session_archive = async_from_sync(session_archive_sync)
session_restore = async_from_sync(session_restore_sync)


def materialize_native_history(project_root: Path, root: Path, spawn_id: str) -> None:
    from meridian.lib.ops.session_target import resolve_session_log_target
    from meridian.lib.ops.session_transcript import iter_source_events
    from meridian.lib.state.history import ingest_portable_history

    if (root / "spawns" / spawn_id / "history.jsonl").exists():
        return
    target = resolve_session_log_target(
        ref=spawn_id, file_path=None, project_root=project_root, runtime_root=root
    )
    source = next(iter(target.sources), None)
    if source is None:
        raise ValueError(f"No transferable transcript available for {spawn_id}")
    ingest_portable_history(root, spawn_id, iter_source_events(source))


def session_stop_maintenance(project_root: Path, chat_id: str) -> str | None:
    """Finite optional policy after stop; no daemon, and no state-to-ops callback."""
    import time

    from meridian.lib.platform.locking import try_lock_file
    from meridian.lib.state.atomic import atomic_write_text

    roots = resolve_roots_for_read(str(project_root))
    if roots is None:
        return None
    try:
        session = session_store.get_session_record(roots.runtime_root, chat_id)
        if session is not None and session.spawn_id:
            materialize_native_history(project_root, roots.runtime_root, session.spawn_id)
        config = load_config(project_root).history.archive
        if not config.automatic:
            return None
        if not config.destination:
            raise ValueError("Automatic history archiving requires history.archive.destination")
        directory = roots.runtime_root / "history-archives"
        with try_lock_file(directory / "automatic.lock") as handle:
            if handle is None:
                return None
            marker = directory / "last-automatic"
            if (
                marker.exists()
                and time.time() - float(marker.read_text()) < config.interval_hours * 3600
            ):
                return None
            result = archive_history(
                roots.runtime_root,
                destination=Path(config.destination),
                eligible=True,
                apply=True,
                policy=config,
                project_root=roots.project_root,
            )
            if result.errors:
                return "History maintenance: " + "; ".join(result.errors)
            atomic_write_text(marker, str(time.time()))
    except (ValueError, OSError, RuntimeError) as exc:
        return f"History maintenance: {exc}"
    return None
