"""Crash-only signal files for resident spawn coordination."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast, get_args

from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

SpawnSignalKind = Literal["done", "rearm"]
_SIGNAL_KINDS: frozenset[str] = frozenset(get_args(SpawnSignalKind))


@dataclass(frozen=True)
class ResidentSignals:
    """Resident coordination signals consumed in one crash-tolerant read."""

    done: bool = False
    rearm: bool = False


def spawn_signal_path(launch: LaunchPaths, kind: SpawnSignalKind) -> Path:
    """Return the authoritative path for a resident spawn signal file."""

    return launch.done_signal if kind == "done" else launch.rearm_signal


def write_accepted_signals(
    launch: LaunchPaths,
    kinds: Iterable[SpawnSignalKind],
) -> None:
    """Declare the signals this spawn's drain plan consumes.

    A spawn-dir file rather than a row field: spawn rows forbid unknown keys, so
    older builds would quarantine every row carrying one.
    """

    payload = json.dumps(sorted(kinds))
    mutate_published_spawn_artifact(
        launch.runtime_root,
        launch.spawn_id,
        lambda: atomic_write_text(launch.accepted_signals, payload),
    )


def read_accepted_signals(launch: LaunchPaths) -> frozenset[SpawnSignalKind] | None:
    """Return the declared signals, or ``None`` when the runner hasn't declared any.

    ``None`` covers a plan not chosen yet, a runner that predates the file, and an
    unreadable file.
    """

    try:
        raw: object = json.loads(launch.accepted_signals.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, list):
        return None
    kinds = cast("list[object]", raw)
    if not all(isinstance(kind, str) and kind in _SIGNAL_KINDS for kind in kinds):
        return None
    return cast("frozenset[SpawnSignalKind]", frozenset(kinds))


def write_spawn_signal(
    launch: LaunchPaths,
    kind: SpawnSignalKind,
    *,
    can_write: Callable[[SpawnRecord], bool] | None = None,
) -> bool:
    """Write an idempotent signal while its published spawn still exists.

    ``can_write`` sees the published row under the same lock as the write.
    """

    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return mutate_published_spawn_artifact(
        launch.runtime_root,
        launch.spawn_id,
        lambda: atomic_write_text(
            spawn_signal_path(launch, kind),
            f"{timestamp}\n",
        ),
        can_mutate=can_write,
    )


def consume_spawn_signal(launch: LaunchPaths, kind: SpawnSignalKind) -> bool:
    """Delete a signal file if present, tolerating races and crash leftovers."""

    path = spawn_signal_path(launch, kind)
    if not path.exists():
        return False
    with suppress(FileNotFoundError):
        path.unlink()
        return True
    return False


def consume_resident_signals(launch: LaunchPaths) -> ResidentSignals:
    """Consume resident done/rearm signal files in a single coordination call."""

    return ResidentSignals(
        done=consume_spawn_signal(launch, "done"),
        rearm=consume_spawn_signal(launch, "rearm"),
    )
