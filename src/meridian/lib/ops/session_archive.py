"""Finite history retention and inert restore policy; files decide reclamation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from graphlib import TopologicalSorter
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
from meridian.lib.state.history_index import HistoryIndex, HistorySnapshot
from meridian.lib.state.process_scope_projection import read_scope_projection
from meridian.lib.state.reaper import scope_liveness
from meridian.lib.state.retention_archive import (
    ArchivedRecord,
    SourceWitness,
    append_receipt,
    archive_locations,
    archive_path,
    capture_record,
    publish_archive,
    read_receipts,
    recover_archives,
    source_witness,
    verified_source,
    verify_archive,
)
from meridian.lib.state.session_identity import (
    native_identity_candidates,
    session_records_for_spawns,
)
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import read_state, write_state_locked
from meridian.lib.state.spawn_aggregate import cleanup_retired_spawn, retire_published_spawn


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
        snapshots: dict[tuple[str, str, bool], list[HistorySnapshot]] = {}
        for snapshot in self.snapshots:
            snapshots.setdefault(
                (snapshot.history_id, snapshot.portable_digest, snapshot.current), []
            ).append(snapshot)
        for (history_id, portable_digest, current), locations in snapshots.items():
            state = "current" if current else "snapshot only"
            lines.append(f"History {history_id} [{state}] {portable_digest}")
            lines.extend(f"  ZIP {row.archive_id}: {row.path}" for row in locations)
        lines.extend(f"Protected: {key}" for key in self.protected)
        lines.extend(f"Error: {error}" for error in self.errors)
        return "\n".join(lines)


def _protected(
    root: Path,
) -> tuple[dict[str, SpawnRecord], set[str], dict[str, set[str]]]:
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
                protected.update(edges[record.id])
            if record.chat_id in chats | fork_chats:
                protected.add(record.id)
        changed = len(protected) != before
    return records, protected, edges


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
    _ = project_root
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
            _, protected, edges = _protected(root)
            sessions = session_records_for_spawns(root, candidates)
        all_candidates = candidates
        by_id = {row.id: row for row in candidates if row.id not in protected}
        dependents: dict[str, set[str]] = {key: set() for key in by_id}
        for key, targets in edges.items():
            for target in targets:
                if key in dependents and target in dependents:
                    dependents[target].add(key)
        # Apply bundle limits after dependency ordering, so small passes still progress.
        candidates = [by_id[key] for key in TopologicalSorter(dependents).static_order()]
        selected: list[ArchivedRecord] = []
        witnesses: dict[str, SourceWitness] = {}
        errors: list[str] = list(recovery_errors)
        limited = False
        selected_bytes = 0
        matched = {
            ref
            for ref in refs
            for row in all_candidates
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
            if len(selected) >= policy.max_records:
                limited = True
                break
            try:
                ready = _capture_ready(root, candidate, sessions.get(candidate.id))
            except ValueError as exc:
                errors.append(f"{candidate.id}: {exc}")
                continue
            if not ready:
                if refs:
                    errors.append(f"{candidate.id}: native snapshot is not captured")
                continue
            if candidate.history_id is None and apply:
                write_state_locked(
                    root / "spawns", candidate.id, lambda row: row, allow_terminal_overwrite=True
                )
            directory = root / "spawns" / candidate.id
            try:
                with verified_source(directory) as witness:
                    state = witness.state
                    if state is None:
                        continue
                    activity = last_activity(state, witness.session, "")
                    if eligible and datetime.fromisoformat(activity) > cutoff:
                        continue
                    record = capture_record(directory, state, witness.session, activity)
            except (ValueError, OSError) as exc:
                errors.append(f"{candidate.id}: {exc}")
                continue
            size = sum(member.size for member in record.files)
            if selected and selected_bytes + size > policy.max_uncompressed_bytes:
                limited = True
                break
            selected.append(record)
            witnesses[record.state.id] = witness
            selected_bytes += size
        if not apply or not selected:
            return SessionArchiveOutput(
                selected=tuple(str(row.history_id) for row in selected),
                protected=tuple(sorted(protected)),
                errors=tuple(errors),
                limited=limited,
            )
        receipt = publish_archive(root, destination, tuple(selected))
        archive = archive_path(receipt)
        verify_archive(archive, tuple(selected))
        reclaimed: list[str] = []
        pending = list(selected)
        while pending:
            with lock_file(changes.mutation_lock):
                records, protected_now, edges = _protected(root)
                # Retire dependents first, including when their dependencies are older.
                # An unselected or changed loose dependent keeps its dependency loose.
                required = {target for targets in edges.values() for target in targets}
                captured = next(
                    (row for row in pending if row.state.id not in required | protected_now),
                    None,
                )
                if captured is None:
                    protected.update(row.state.id for row in pending)
                    break
                pending.remove(captured)
                current = records.get(captured.state.id)
                if current is None or current.id in protected_now:
                    continue
                source = HistorySource(kind="spawn", key=current.id)
                with lock_file(source.lock_path(root)):
                    try:
                        unchanged = (
                            source_witness(root / "spawns" / current.id) == witnesses[current.id]
                        )
                    except OSError:
                        unchanged = False
                    if not unchanged:
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
                    retired = retire_published_spawn(
                        root,
                        current.id,
                        can_delete=lambda row, captured=captured: (
                            row is not None and row.history_id == captured.history_id
                        ),
                    )
            # No root, spawn or process-scope locks span recursive cleanup.
            if retired is not None and cleanup_retired_spawn(retired):
                append_receipt(
                    root,
                    receipt.model_copy(update={"event": "reclaimed", "records": (captured,)}),
                )
                reclaimed.append(str(captured.history_id))
            else:
                errors.append(f"{current.id}: reclaim cleanup incomplete; verified ZIP retained")
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


def _capture_ready(root: Path, candidate: SpawnRecord, session: object) -> bool:
    from meridian.lib.state.native_snapshot import (
        NATIVE_SNAPSHOT_FILENAME,
        canonical_transcript_path,
        published_snapshot_state,
    )

    spawn_dir = root / "spawns" / candidate.id
    transcript = canonical_transcript_path(spawn_dir)
    if transcript is not None and transcript.name == NATIVE_SNAPSHOT_FILENAME:
        state = published_snapshot_state(transcript, history_id=candidate.history_id)
        if state == "complete":
            return True
        raise ValueError("published native snapshot is corrupt")
    linked = session if isinstance(session, session_store.SessionRecord) else None
    harnesses, native_ids = native_identity_candidates(root, candidate, linked)
    # A runner stream is neither proof of native binding nor an archive source.
    # Native-bound records become eligible only after stop maintenance seals the
    # exact source as a snapshot above.
    if not harnesses or not native_ids:
        raise ValueError("no exact native source is bound; record remains loose")
    return False


def _require_inactive_native_session(root: Path, harness: str | None, session_id: str) -> None:
    """Reject known same-runtime owners; this is not an external-writer fence."""
    scan = spawn_store.list_spawns(root)
    if scan.quarantines:
        raise ValueError("Cannot establish native capture ownership with quarantined spawn records")
    linked = session_records_for_spawns(root, scan.records)
    for row in scan.records:
        if row.record_mode == "historical":
            continue
        session = linked.get(row.id)
        harnesses, native_ids = native_identity_candidates(root, row, session)
        if harness not in harnesses or session_id not in native_ids:
            continue
        scopes = read_scope_projection(root, SpawnId(row.id))
        if (
            row.status not in TERMINAL_SPAWN_STATUSES
            or (
                session is not None
                and (
                    session.stopped_at is None
                    or session_store.is_session_lease_owner_alive(
                        root, session.chat_id, session_instance_id=session.session_instance_id
                    )
                )
            )
            or any(
                scope_liveness(scope)["likely_serving"]
                for scope in scopes.scopes
                if scope.release_id not in scopes.released_ids
            )
        ):
            raise ValueError(f"Cannot capture an active native owner: {row.id}")
    for session in session_store.list_all_session_records(root):
        if (
            session.record_mode != "historical"
            and session.harness.strip().lower() == harness
            and session.harness_session_id == session_id
            and (
                session.stopped_at is None
                or session_store.is_session_lease_owner_alive(
                    root, session.chat_id, session_instance_id=session.session_instance_id
                )
            )
        ):
            raise ValueError(f"Cannot capture an active native owner: {session.chat_id}")


def materialize_native_history(project_root: Path, root: Path, spawn_id: str) -> None:
    from meridian.lib.harness.transcript_capture import native_capture
    from meridian.lib.launch.constants import HISTORY_FILENAME
    from meridian.lib.ops.session_target import resolve_transcript_source
    from meridian.lib.platform.atomic import atomic_replace, iter_atomic_temp_paths
    from meridian.lib.state.event_store import utc_now_iso
    from meridian.lib.state.history_codec import transcript_header
    from meridian.lib.state.native_snapshot import (
        NATIVE_SNAPSHOT_FILENAME,
        SnapshotHeader,
        SnapshotObservation,
        published_snapshot_state,
        write_snapshot,
    )
    from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

    def capture() -> None:
        state = read_state(root / "spawns", spawn_id, include_prompt=False)
        if state is None or state.history_id is None or state.status not in TERMINAL_SPAWN_STATUSES:
            raise ValueError("Native transcript capture requires an identified terminal record")
        snapshot = root / "spawns" / spawn_id / NATIVE_SNAPSHOT_FILENAME
        snapshot_state = published_snapshot_state(snapshot, history_id=state.history_id)
        if snapshot_state == "complete":
            return
        if snapshot_state == "corrupt":
            raise ValueError("Published native snapshot is corrupt")
        # Remove stale staging temps left by a previous interrupted capture.
        spawn_dir = root / "spawns" / spawn_id
        for reserved in (NATIVE_SNAPSHOT_FILENAME, HISTORY_FILENAME):
            for stale in iter_atomic_temp_paths(spawn_dir, reserved):
                stale.unlink(missing_ok=True)
        # Deferred until the published-aggregate guard: select from current authority.
        target = resolve_transcript_source(
            ref=spawn_id,
            file_path=None,
            project_root=project_root,
            runtime_root=root,
            purpose="capture",
        )
        source = target.source
        observation = native_capture(
            kind=source.kind,
            harness=source.harness,
            session_id=source.session_id,
            path=source.path,
        )
        if observation.status != "complete":
            raise ValueError(observation.reason or observation.status)
        _require_inactive_native_session(root, source.harness, source.session_id)
        HistoryChanges(root).mark(HistorySource(kind="spawn", key=spawn_id))
        header = SnapshotHeader(
            transcript=transcript_header(state, root.name),
            session_instance_id=state.session_instance_id,
            harness=observation.harness,
            native_session_id=source.session_id,
            dialect=observation.dialect,
            scope=observation.scope,
            observed_from=observation.observed_from or utc_now_iso(),
        )
        with atomic_replace(snapshot, mode="wb", encoding=None, permissions=0o600) as handle:

            def finish() -> SnapshotObservation:
                _require_inactive_native_session(root, source.harness, source.session_id)
                return observation.finish()

            write_snapshot(handle, header, observation.records(), finish)

    if not mutate_published_spawn_artifact(root, SpawnId(spawn_id), capture):
        raise ValueError(f"Native capture target is missing or historical: {spawn_id}")


def session_stop_maintenance(project_root: Path, primary_spawn_id: str) -> str | None:
    """Maintain the completed aggregate; latest chat may already name another run."""
    import time

    from meridian.lib.platform.locking import try_lock_file
    from meridian.lib.state.atomic import atomic_write_text

    roots = resolve_roots_for_read(str(project_root))
    if roots is None:
        return None
    try:
        materialize_native_history(project_root, roots.runtime_root, primary_spawn_id)
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
