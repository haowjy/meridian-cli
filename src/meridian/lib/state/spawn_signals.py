"""Crash-only signal files for resident spawn coordination."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from meridian.lib.core.types import SpawnId
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import spawn_log_subpath
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

SpawnSignalKind = Literal["done", "rearm"]


@dataclass(frozen=True)
class ResidentSignals:
    """Resident coordination signals consumed in one crash-tolerant read."""

    done: bool = False
    rearm: bool = False


def spawn_signal_path(runtime_root: Path, spawn_id: SpawnId | str, kind: SpawnSignalKind) -> Path:
    """Return the authoritative path for a resident spawn signal file."""

    return runtime_root / spawn_log_subpath(spawn_id) / f"{kind}.signal"


def write_spawn_signal(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    kind: SpawnSignalKind,
) -> bool:
    """Write an idempotent signal while its published spawn still exists."""

    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return mutate_published_spawn_artifact(
        runtime_root,
        spawn_id,
        lambda: atomic_write_text(
            spawn_signal_path(runtime_root, spawn_id, kind),
            f"{timestamp}\n",
        ),
    )


def consume_spawn_signal(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    kind: SpawnSignalKind,
) -> bool:
    """Delete a signal file if present, tolerating races and crash leftovers."""

    path = spawn_signal_path(runtime_root, spawn_id, kind)
    if not path.exists():
        return False
    with suppress(FileNotFoundError):
        path.unlink()
        return True
    return False


def consume_resident_signals(runtime_root: Path, spawn_id: SpawnId | str) -> ResidentSignals:
    """Consume resident done/rearm signal files in a single coordination call."""

    return ResidentSignals(
        done=consume_spawn_signal(runtime_root, spawn_id, "done"),
        rearm=consume_spawn_signal(runtime_root, spawn_id, "rearm"),
    )
