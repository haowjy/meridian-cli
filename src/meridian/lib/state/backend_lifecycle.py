"""Canonical schema and I/O helpers for backend lifecycle sidecar."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from meridian.lib.launch.constants import BACKEND_LIFECYCLE_FILENAME
from meridian.lib.platform.process_scope import ProcessScopeSnapshot
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.process_scope_projection import scope_snapshot_from_dict


@dataclass(frozen=True)
class BackendLifecycleRecord:
    """Canonical schema for backend_lifecycle.json."""

    phase: str
    phase_entered_epoch: float
    phase_timeout_seconds: float
    backend_pid: int
    backend_birth_epoch: float
    scope_snapshot: ProcessScopeSnapshot
    harness_session_id: str | None
    parent_death_linked: bool


def backend_lifecycle_path(
    *,
    runtime_root: Path | None = None,
    spawn_dir: Path | None = None,
    spawn_id: str | None = None,
) -> Path:
    """Resolve path to backend_lifecycle.json.

    Exactly one of spawn_dir or (runtime_root, spawn_id) must be provided.
    """

    has_spawn_dir = spawn_dir is not None
    has_runtime_pair = runtime_root is not None or spawn_id is not None

    if has_spawn_dir and has_runtime_pair:
        raise ValueError("Provide either spawn_dir or runtime_root+spawn_id, not both")
    if has_spawn_dir:
        return spawn_dir / BACKEND_LIFECYCLE_FILENAME
    if runtime_root is None or spawn_id is None:
        raise ValueError("runtime_root and spawn_id are required when spawn_dir is not provided")
    return runtime_root / "spawns" / spawn_id / BACKEND_LIFECYCLE_FILENAME


def _coerce_positive_int(value: object) -> int | None:
    if not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return value


def _coerce_nonnegative_float(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    if normalized < 0.0:
        return None
    return normalized


def _coerce_optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def read_backend_lifecycle(runtime_root: Path, spawn_id: str) -> BackendLifecycleRecord | None:
    """Tolerant read with crash-only semantics. Returns None for missing/corrupt file."""

    lifecycle_path = backend_lifecycle_path(runtime_root=runtime_root, spawn_id=spawn_id)
    if not lifecycle_path.is_file():
        return None
    try:
        payload_obj = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload_obj, dict):
        return None

    payload = cast("dict[str, object]", payload_obj)
    phase = payload.get("phase")
    if not isinstance(phase, str) or not phase.strip():
        return None

    phase_entered_epoch = _coerce_nonnegative_float(payload.get("phase_entered_epoch"))
    phase_timeout_seconds = _coerce_nonnegative_float(payload.get("phase_timeout_seconds"))
    backend_pid = _coerce_positive_int(payload.get("backend_pid"))
    backend_birth_epoch = _coerce_nonnegative_float(payload.get("backend_birth_epoch"))
    scope_snapshot_raw = payload.get("scope_snapshot")
    scope_snapshot = (
        scope_snapshot_from_dict(cast("dict[str, object]", scope_snapshot_raw))
        if isinstance(scope_snapshot_raw, dict)
        else None
    )
    parent_death_linked = payload.get("parent_death_linked")

    if (
        phase_entered_epoch is None
        or phase_timeout_seconds is None
        or backend_pid is None
        or backend_birth_epoch is None
        or scope_snapshot is None
        or not isinstance(parent_death_linked, bool)
    ):
        return None

    harness_session_id = _coerce_optional_text(payload.get("harness_session_id"))

    return BackendLifecycleRecord(
        phase=phase,
        phase_entered_epoch=phase_entered_epoch,
        phase_timeout_seconds=phase_timeout_seconds,
        backend_pid=backend_pid,
        backend_birth_epoch=backend_birth_epoch,
        scope_snapshot=scope_snapshot,
        harness_session_id=harness_session_id,
        parent_death_linked=parent_death_linked,
    )


def write_backend_lifecycle(spawn_dir: Path, record: BackendLifecycleRecord) -> None:
    """Atomic write via tmp+rename."""

    payload = {
        "phase": record.phase,
        "phase_entered_epoch": record.phase_entered_epoch,
        "phase_timeout_seconds": record.phase_timeout_seconds,
        "backend_pid": record.backend_pid,
        "backend_birth_epoch": record.backend_birth_epoch,
        "scope_snapshot": dataclasses.asdict(record.scope_snapshot),
        "harness_session_id": record.harness_session_id,
        "parent_death_linked": record.parent_death_linked,
    }
    atomic_write_text(
        backend_lifecycle_path(spawn_dir=spawn_dir),
        json.dumps(payload, separators=(",", ":")) + "\n",
    )


__all__ = [
    "BackendLifecycleRecord",
    "backend_lifecycle_path",
    "read_backend_lifecycle",
    "write_backend_lifecycle",
]
