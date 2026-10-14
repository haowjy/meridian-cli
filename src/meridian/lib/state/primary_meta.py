"""Canonical schema and I/O helpers for managed primary metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict

from meridian.lib.core.types import SpawnId
from meridian.lib.launch.constants import PRIMARY_META_FILENAME
from meridian.lib.state.json_doc import mutate_model, read_model, write_model
from meridian.lib.state.spawn_aggregate import (
    PublishedArtifactUnavailable,
    published_artifact_lock,
)
from meridian.lib.utils.json_shapes import is_object_sequence

ActivityState = Literal["starting", "idle", "turn_active", "finalizing"]


def _positive_int_or_none(value: object) -> object:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _int_or_none(value: object) -> object:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _nonnegative_float_or_none(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    normalized = float(value)
    return normalized if normalized >= 0.0 else None


def _activity_or_none(value: object) -> object:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in {"starting", "idle", "turn_active", "finalizing"} else None


def _text_or_none(value: object) -> object:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _command_or_none(value: object) -> object:
    if not is_object_sequence(value):
        return None
    command: list[str] = []
    for token in value:
        if not isinstance(token, str) or not token.strip():
            return None
        command.append(token.strip())
    return tuple(command) or None


type _PositiveInt = Annotated[int | None, BeforeValidator(_positive_int_or_none)]
type _OptionalInt = Annotated[int | None, BeforeValidator(_int_or_none)]
type _NonnegativeFloat = Annotated[float | None, BeforeValidator(_nonnegative_float_or_none)]
type _OptionalActivity = Annotated[ActivityState | None, BeforeValidator(_activity_or_none)]
type _OptionalText = Annotated[str | None, BeforeValidator(_text_or_none)]
type _OptionalCommand = Annotated[tuple[str, ...] | None, BeforeValidator(_command_or_none)]


class PrimaryMetadata(BaseModel):
    """Canonical schema for primary_meta.json."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    managed_backend: bool
    launcher_pid: _PositiveInt = None
    launcher_birth_epoch: _NonnegativeFloat = None
    backend_pid: _PositiveInt = None
    backend_birth_epoch: _NonnegativeFloat = None
    tui_pid: _PositiveInt = None
    tui_birth_epoch: _NonnegativeFloat = None
    backend_port: _PositiveInt = None
    activity: _OptionalActivity = None
    harness_session_id: _OptionalText = None
    command: _OptionalCommand = None
    launch_cwd: _OptionalText = None
    started_at_epoch: _NonnegativeFloat = None
    ended_at_epoch: _NonnegativeFloat = None
    exit_code: _OptionalInt = None
    runtime_kind: _OptionalText = None
    runtime_path: _OptionalText = None
    runtime_version: _OptionalText = None
    session_dir: _OptionalText = None
    auth_policy: _OptionalText = None

@dataclass(frozen=True)
class PrimarySurfaceMetadata:
    """Projection for spawn list/show surfaces."""

    managed_backend: bool
    activity: str | None
    backend_pid: int | None
    tui_pid: int | None
    backend_port: int | None
    harness_session_id: str | None
    command: tuple[str, ...] | None
    launch_cwd: str | None
    started_at_epoch: float | None
    ended_at_epoch: float | None
    exit_code: int | None
    runtime_kind: str | None
    runtime_path: str | None
    runtime_version: str | None
    session_dir: str | None
    auth_policy: str | None

def primary_meta_path(
    *,
    runtime_root: Path | None = None,
    spawn_dir: Path | None = None,
    spawn_id: str | None = None,
) -> Path:
    """Resolve path to primary_meta.json.

    Exactly one of spawn_dir or (runtime_root, spawn_id) must be provided.
    """

    has_spawn_dir = spawn_dir is not None
    has_runtime_pair = runtime_root is not None or spawn_id is not None

    if has_spawn_dir and has_runtime_pair:
        raise ValueError("Provide either spawn_dir or runtime_root+spawn_id, not both")
    if has_spawn_dir:
        return spawn_dir / PRIMARY_META_FILENAME
    if runtime_root is None or spawn_id is None:
        raise ValueError("runtime_root and spawn_id are required when spawn_dir is not provided")
    return runtime_root / "spawns" / spawn_id / PRIMARY_META_FILENAME

def read_primary_metadata(runtime_root: Path, spawn_id: str) -> PrimaryMetadata | None:
    """Tolerant read with crash-only semantics. Returns None for missing/corrupt file."""

    return read_model(
        primary_meta_path(runtime_root=runtime_root, spawn_id=spawn_id),
        PrimaryMetadata,
    )

def write_primary_metadata(
    spawn_dir: Path,
    metadata: PrimaryMetadata,
    *,
    runtime_root: Path | None = None,
    spawn_id: str | None = None,
) -> bool:
    """Atomic write via tmp+rename."""

    if (runtime_root is None) is not (spawn_id is None):
        raise ValueError("runtime_root and spawn_id must be provided together")

    path = primary_meta_path(spawn_dir=spawn_dir)
    if runtime_root is None or spawn_id is None:
        write_model(path, metadata)
        return True
    try:
        mutate_model(
            path,
            PrimaryMetadata,
            lambda _current: (metadata, None),
            lock=published_artifact_lock(runtime_root, SpawnId(spawn_id)),
        )
    except PublishedArtifactUnavailable:
        return False
    return True

def read_primary_surface_metadata(runtime_root: Path, spawn_id: str) -> PrimarySurfaceMetadata:
    """Read projection for CLI surfaces. Returns defaults if file missing."""

    metadata = read_primary_metadata(runtime_root, spawn_id)
    if metadata is None:
        return PrimarySurfaceMetadata(
            managed_backend=False,
            activity=None,
            backend_pid=None,
            tui_pid=None,
            backend_port=None,
            harness_session_id=None,
            command=None,
            launch_cwd=None,
            started_at_epoch=None,
            ended_at_epoch=None,
            exit_code=None,
            runtime_kind=None,
            runtime_path=None,
            runtime_version=None,
            session_dir=None,
            auth_policy=None,
        )
    return PrimarySurfaceMetadata(
        managed_backend=metadata.managed_backend,
        activity=metadata.activity,
        backend_pid=metadata.backend_pid,
        tui_pid=metadata.tui_pid,
        backend_port=metadata.backend_port,
        harness_session_id=metadata.harness_session_id,
        command=metadata.command,
        launch_cwd=metadata.launch_cwd,
        started_at_epoch=metadata.started_at_epoch,
        ended_at_epoch=metadata.ended_at_epoch,
        exit_code=metadata.exit_code,
        runtime_kind=metadata.runtime_kind,
        runtime_path=metadata.runtime_path,
        runtime_version=metadata.runtime_version,
        session_dir=metadata.session_dir,
        auth_policy=metadata.auth_policy,
    )

def read_primary_harness_session_id(runtime_root: Path, spawn_id: str) -> str | None:
    """Read harness_session_id only. Used by session_log resolution."""

    metadata = read_primary_metadata(runtime_root, spawn_id)
    if metadata is None:
        return None
    return metadata.harness_session_id


def is_managed_primary(runtime_root: Path, spawn_id: str) -> bool:
    """Check if spawn is a managed primary."""

    metadata = read_primary_metadata(runtime_root, spawn_id)
    return bool(metadata is not None and metadata.managed_backend)


__all__ = [
    "ActivityState",
    "PrimaryMetadata",
    "PrimarySurfaceMetadata",
    "is_managed_primary",
    "primary_meta_path",
    "read_primary_harness_session_id",
    "read_primary_metadata",
    "read_primary_surface_metadata",
    "write_primary_metadata",
]
