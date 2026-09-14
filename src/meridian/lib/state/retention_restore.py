"""Selective ZIP restore: staged files plus one inert historical session event.

The per-history plan is the retry key across directory publication and session
append. It never creates leases, processes, cleanup claims, or live sessions.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.types import ChatId
from meridian.lib.platform.atomic import atomic_replace
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_publish_dir, atomic_write_text
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.retention_archive import (
    _PREFIX,
    ArchivedRecord,
    ArchiveReceipt,
    _location,
    append_receipt,
    archive_manifest_digest,
    digest,
    inventory,
    portable_digest,
    safe_member_name,
    verify_archive,
)
from meridian.lib.state.session_identity import session_records_for_spawns
from meridian.lib.state.session_store import (
    SessionRecord,
    append_historical_session,
    reserve_chat_id,
)
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import read_state, record_to_stored_state
from meridian.lib.state.spawn_store import list_spawns, reserve_spawn_id


class RestorePlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    portable_digest: str
    local_id: str
    chat_id: str
    generation: str
    session: SessionRecord


def _historical_session(
    record: ArchivedRecord, chat_id: str, spawn_id: str, generation: str
) -> SessionRecord:
    if record.session is not None:
        return record.session.model_copy(
            update={
                "chat_id": ChatId(chat_id),
                "history_id": record.history_id,
                "record_mode": "historical",
                "spawn_id": spawn_id,
                "session_instance_id": generation,
                "stopped_at": record.session.stopped_at or record.activity,
                "harness_session_id": None,
                "harness_session_ids": (),
                "control_root": None,
                "task_cwd": None,
                "execution_cwd": None,
                "claude_config_dir": None,
            }
        )
    state = record.state
    return SessionRecord(
        chat_id=ChatId(chat_id),
        history_id=record.history_id,
        record_mode="historical",
        kind="primary" if state.kind == "primary" else "spawn",
        harness=state.harness or "",
        harness_session_id=None,
        harness_session_ids=(),
        model=state.model or "",
        agent=state.agent or "",
        agent_path=state.agent_path or "",
        skills=state.skills,
        skill_paths=(),
        params=(),
        started_at=state.started_at or record.activity,
        stopped_at=record.activity,
        session_instance_id=generation,
        spawn_id=spawn_id,
        active_work_id=record.session.active_work_id if record.session else state.work_id,
    )


def _verify_existing(directory: Path, record: ArchivedRecord) -> None:
    marker = directory / "restored-from.json"
    actual = inventory(directory)
    if not marker.exists():
        current = read_state(directory.parent, directory.name, include_prompt=False)
        assert current is not None
        session = session_records_for_spawns(directory.parent.parent, (current,)).get(current.id)
        if (
            actual != record.files
            or portable_digest(current, actual, session) != record.portable_digest
        ):
            raise ValueError(f"History identity conflict: {record.history_id}")
        return
    saved = json.loads(marker.read_text())
    if saved["portable_digest"] != record.portable_digest:
        raise ValueError(f"History identity conflict: {record.history_id}")
    state_member = next(member for member in actual if member.name == "state.json")
    provenance_member = next((member for member in actual if member.name == "record.json"), None)
    if (
        state_member.sha256 != saved["state_sha256"]
        or provenance_member is None
        or provenance_member.sha256 != saved["provenance_sha256"]
    ):
        raise ValueError(f"Restored metadata changed: {record.history_id}")
    # Restore changes only local lifecycle and provenance. All content must match,
    # including membership: additional files are a conflict, not silently ignored.
    excluded = {"state.json", "record.json"}
    if tuple(m for m in actual if m.name not in excluded) != tuple(
        m for m in record.files if m.name not in excluded
    ):
        raise ValueError(f"Restored content changed: {record.history_id}")


def _stage_record(
    root: Path, archive_path: Path, archive_id: UUID, record: ArchivedRecord, plan: RestorePlan
) -> Path:
    """Copy and hash external bytes without holding the root mutation gate."""
    stage = root / "history-archives" / "staging" / f"restore-{uuid4().hex}"
    stage.mkdir(parents=True, mode=0o700)
    with zipfile.ZipFile(archive_path) as archive:
        for member in record.files:
            relative = safe_member_name(member.name)
            if relative in {"state.json", "record.json", "restored-from.json"}:
                continue
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with (
                archive.open(
                    f"{_PREFIX}records/{record.history_id}/aggregate/{relative}"
                ) as incoming,
                atomic_replace(target, mode="wb", encoding=None, permissions=0o600) as outgoing,
            ):
                checksum = hashlib.sha256()
                size = 0
                while chunk := incoming.read(1024 * 1024):
                    checksum.update(chunk)
                    size += len(chunk)
                    outgoing.write(chunk)
                if size != member.size or checksum.hexdigest() != member.sha256:
                    raise ValueError("Archive changed during restore extraction")
    state = record.state.model_copy(
        update={
            "id": plan.local_id,
            "chat_id": ChatId(plan.chat_id),
            "owner_chat_id": None,
            "parent_id": None,
            "record_mode": "historical",
            "state_revision": 1,
            "session_instance_id": plan.generation,
            "worker_pid": None,
            "runner_pid": None,
            "runner_created_at_epoch": None,
            "originating_bash_id": None,
            "runner_exit": None,
            "cancel_intent": None,
            "launch_mode": None,
            "launch_policy_snapshot": None,
            "control_root": None,
            "task_cwd": None,
            "execution_cwd": None,
            "claude_config_dir": None,
            "harness_session_id": None,
            "resident_rearm_count": 0,
            "status": record.state.status
            if record.state.status in TERMINAL_SPAWN_STATUSES
            else "unknown",
            "terminal": record.state.terminal
            if record.state.status in TERMINAL_SPAWN_STATUSES
            else None,
        }
    )
    atomic_write_text(stage / "state.json", record_to_stored_state(state).model_dump_json())
    atomic_write_text(stage / "record.json", record.model_dump_json())
    atomic_write_text(
        stage / "restored-from.json",
        json.dumps(
            {
                "archive_id": str(archive_id),
                "portable_digest": record.portable_digest,
                "state_sha256": digest((stage / "state.json").read_bytes()),
                "provenance_sha256": digest((stage / "record.json").read_bytes()),
            }
        ),
    )
    return stage


def _existing_record(root: Path, history_id: UUID) -> SpawnRecord | None:
    scan = list_spawns(root)
    if scan.quarantines:
        raise ValueError("Cannot resolve restore conflicts with quarantined records")
    matches = [row for row in scan.records if row.history_id == history_id]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous local history identity: {history_id}")
    return matches[0] if matches else None


def restore_archive(root: Path, archive_path: Path, refs: tuple[str, ...]) -> tuple[str, ...]:
    if not refs:
        raise ValueError("Select one or more history IDs or origin aliases to restore")
    manifest_hash = archive_manifest_digest(archive_path)
    manifest = verify_archive(archive_path, manifest_sha256=manifest_hash)
    selected = tuple(
        record
        for record in manifest.records
        if set(refs)
        & {
            str(record.history_id),
            record.state.id,
            record.state.chat_id,
        }
    )
    matched = {
        ref
        for ref in refs
        if any(ref in {str(row.history_id), row.state.id, row.state.chat_id} for row in selected)
    }
    if set(refs) != matched:
        raise ValueError(f"Archive references not found: {sorted(set(refs) - matched)}")
    changes = HistoryChanges(root)
    restored: list[str] = []
    with lock_file(root / "history-archives/archive.lock"):
        plans = root / "history-archives" / "restores"
        for record in selected:
            plan_path = plans / f"{record.history_id}.json"
            with lock_file(changes.mutation_lock):
                existing = _existing_record(root, record.history_id)
                if existing is not None and not plan_path.exists():
                    _verify_existing(root / "spawns" / existing.id, record)
                    restored.append(existing.id)
                    continue
                if plan_path.exists():
                    plan = RestorePlan.model_validate_json(plan_path.read_bytes())
                    if plan.portable_digest != record.portable_digest:
                        raise ValueError("Unfinished restore conflicts with selected content")
                else:
                    local_id = str(reserve_spawn_id(root))
                    chat_id = reserve_chat_id(root)
                    generation = uuid4().hex
                    plan = RestorePlan(
                        portable_digest=record.portable_digest,
                        local_id=local_id,
                        chat_id=chat_id,
                        generation=generation,
                        session=_historical_session(record, chat_id, local_id, generation),
                    )
                    atomic_write_text(plan_path, plan.model_dump_json())
                destination = root / "spawns" / plan.local_id
                source = HistorySource(kind="spawn", key=plan.local_id)
                source_lock = source.lock_path(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
            stage = (
                None
                if existing is not None
                else _stage_record(root, archive_path, manifest.archive_id, record, plan)
            )
            with lock_file(changes.mutation_lock):
                # Writers may have progressed while external bytes were staged.
                existing = _existing_record(root, record.history_id)
                if existing is not None and existing.id != plan.local_id:
                    raise ValueError("Restore history identity changed during staging")
                with lock_file(source_lock):
                    current = read_state(root / "spawns", plan.local_id, include_prompt=False)
                    if current is not None:
                        if (
                            current.history_id != record.history_id
                            or current.record_mode != "historical"
                        ):
                            raise ValueError("Restore publication alias conflict")
                        _verify_existing(destination, record)
                    else:
                        if stage is None:
                            raise ValueError(
                                "Existing restore disappeared; retry from its durable plan"
                            )
                        changes.mark(source)
                        atomic_publish_dir(stage, destination)
                append_historical_session(root, plan.session)
                plan_path.unlink()
                restored.append(plan.local_id)
        # Keep a local receipt so copying only the ZIP also reconstructs catalog facts.
        location_id = _location(archive_path.parent)
        append_receipt(
            root,
            ArchiveReceipt(
                event="imported",
                archive_id=manifest.archive_id,
                location_id=location_id,
                destination=str(archive_path.parent.resolve()),
                zip_name=archive_path.name,
                manifest_sha256=manifest_hash,
                records=selected,
            ),
        )
    return tuple(restored)
