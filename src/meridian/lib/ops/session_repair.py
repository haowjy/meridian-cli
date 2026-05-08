"""Explicit session transcript reference repair/persistence helpers."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.project_root import resolve_project_root
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.ops.session_target import resolve_session_log_target
from meridian.lib.ops.spawn.query import (
    read_latest_primary_spawn_for_chat_read_only,
    read_spawn_row_read_only,
)
from meridian.lib.state import session_store, spawn_store


class SessionRepairInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str = ""
    file_path: str | None = None
    project_root: str | None = None


class SessionRepairOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    source: str
    session_record_updated: bool = False
    spawn_record_updated: bool = False


def _is_chat_ref(value: str) -> bool:
    return value.startswith("c") and value[1:].isdigit()


def _is_spawn_ref(value: str) -> bool:
    return value.startswith("p") and value[1:].isdigit()


def _chat_record(runtime_root: Path, chat_id: str) -> session_store.SessionRecord | None:
    records = session_store.get_session_records(runtime_root, {chat_id})
    if not records:
        return None
    return records[0]


def repair_session_reference_sync(payload: SessionRepairInput) -> SessionRepairOutput:
    if payload.file_path is not None and payload.file_path.strip():
        raise ValueError("session repair requires REF; --file is read-only and cannot be repaired")

    ref = payload.ref.strip()
    if not ref:
        raise ValueError("session repair requires a session reference")

    explicit_project_root = (
        Path(payload.project_root).expanduser().resolve() if payload.project_root else None
    )
    project_root = resolve_project_root(explicit_project_root)
    runtime_root = resolve_runtime_root_for_read(project_root)
    target = resolve_session_log_target(
        ref=ref,
        file_path=None,
        project_root=project_root,
        runtime_root=runtime_root,
    )

    session_record_updated = False
    spawn_record_updated = False

    if _is_chat_ref(ref):
        chat_record = _chat_record(runtime_root, ref)
        if chat_record is not None and chat_record.harness_session_id.strip() != target.session_id:
            session_store.update_session_harness_id(runtime_root, ref, target.session_id)
            session_record_updated = True

        primary_spawn = read_latest_primary_spawn_for_chat_read_only(
            project_root,
            ref,
            runtime_root=runtime_root,
        )
        if (
            primary_spawn is not None
            and (primary_spawn.harness_session_id or "").strip() != target.session_id
        ):
            spawn_store.update_spawn(
                runtime_root,
                primary_spawn.id,
                harness_session_id=target.session_id,
            )
            spawn_record_updated = True

    elif _is_spawn_ref(ref):
        spawn_row = read_spawn_row_read_only(
            project_root,
            ref,
            runtime_root=runtime_root,
        )
        if (
            spawn_row is not None
            and (spawn_row.harness_session_id or "").strip() != target.session_id
        ):
            spawn_store.update_spawn(runtime_root, ref, harness_session_id=target.session_id)
            spawn_record_updated = True

        chat_id = spawn_row.chat_id if spawn_row is not None else None
        if chat_id:
            chat_record = _chat_record(runtime_root, chat_id)
            if (
                chat_record is not None
                and chat_record.harness_session_id.strip() != target.session_id
            ):
                session_store.update_session_harness_id(runtime_root, chat_id, target.session_id)
                session_record_updated = True

    return SessionRepairOutput(
        session_id=target.session_id,
        source=target.source,
        session_record_updated=session_record_updated,
        spawn_record_updated=spawn_record_updated,
    )


__all__ = [
    "SessionRepairInput",
    "SessionRepairOutput",
    "repair_session_reference_sync",
]
