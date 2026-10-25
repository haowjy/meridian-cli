"""Crash-safe consumption and wait-lease state for spawn results."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

import psutil
from pydantic import Field, StrictInt

from meridian.lib.core.types import SpawnId
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.json_doc import VersionedPrivateDoc, mutate_model, read_model
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.paths import observed_spawns_path
from meridian.lib.state.spawn.repository import SpawnStateQuarantined
from meridian.lib.state.spawn_aggregate import (
    PublishedArtifactUnavailable,
    published_artifact_lock,
)


class WaitReservation(VersionedPrivateDoc):
    owner_pid: StrictInt = Field(gt=0)
    owner_birth_epoch: float = Field(gt=0)
    expires_at_epoch: float
    spawn_ids: list[str]


class SpawnObservations(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float = 0.0
    observed_spawn_ids: list[str]
    waiting_spawn_ids: list[str] = Field(default_factory=list)
    wait_reservations: dict[str, WaitReservation] = Field(default_factory=dict)


def _read(path: Path, parent_spawn_id: str) -> SpawnObservations | None:
    observations = read_model(path, SpawnObservations)
    if observations is None or observations.spawn_id != parent_spawn_id:
        return None
    return observations


# legacy-observed-spawns (#719): remove this helper with the compatibility reader.
def _legacy_dir(runtime_root: Path, parent_spawn_id: str) -> Path:
    return runtime_root / "pi-bash" / parent_spawn_id


def read_legacy(runtime_root: Path, parent_spawn_id: str) -> SpawnObservations | None:
    """legacy-observed-spawns (#719): Read the old file; damage counts as absent.

    Builds from before the move wrote ``pi-bash/<parent>/observed-spawns.json``, and
    a ``spawn wait`` started by one may still be writing it. Nothing here deletes it.
    """

    legacy_path = _legacy_dir(runtime_root, parent_spawn_id) / "observed-spawns.json"
    return _read(legacy_path, parent_spawn_id)


def _documents(runtime_root: Path, parent_spawn_id: str) -> tuple[SpawnObservations, ...]:
    found = (
        _read(observed_spawns_path(runtime_root, parent_spawn_id), parent_spawn_id),
        read_legacy(runtime_root, parent_spawn_id),  # legacy-observed-spawns (#719)
    )
    return tuple(document for document in found if document is not None)


def record(
    *,
    runtime_root: Path,
    parent_spawn_id: str | None,
    waiting_add: Iterable[str] = (),
    waiting_remove: Iterable[str] = (),
    observed_add: Iterable[str] = (),
    lease_seconds: float = 30.0,
    lock_timeout: float | None = None,
) -> bool:
    """Record results consumed by a parent or leased by its explicit wait.

    Returns ``False`` when the parent's published row is gone or unreadable, so
    nothing was written. ``spawn ack`` treats that as an error; ``spawn wait``
    proceeds without a record because no reader selects that parent's children.
    A finite ``lock_timeout`` bounds best-effort shown-result observation.
    """

    waiting_add_ids = tuple(waiting_add)
    waiting_remove_ids = tuple(waiting_remove)
    observed_add_ids = tuple(observed_add)
    if not parent_spawn_id or not (
        waiting_add_ids or waiting_remove_ids or observed_add_ids
    ):
        return True

    path = observed_spawns_path(runtime_root, parent_spawn_id)

    def update(existing: SpawnObservations | None) -> tuple[SpawnObservations, None]:
        existing = existing or SpawnObservations(
            v=1,
            spawn_id=parent_spawn_id,
            observed_spawn_ids=[],
        )
        observed_ids = set(existing.observed_spawn_ids)
        now = time.time()
        reservations = {
            token: lease
            for token, lease in existing.wait_reservations.items()
            if lease.expires_at_epoch > now
        }
        token = f"{os.getpid()}:{threading.get_ident()}"
        own_lease = reservations.get(token)
        own_ids = set(own_lease.spawn_ids if own_lease else ())
        own_ids.update(waiting_add_ids)
        own_ids.difference_update(waiting_remove_ids)
        if own_ids:
            reservations[token] = WaitReservation(
                owner_pid=os.getpid(),
                owner_birth_epoch=psutil.Process().create_time(),
                expires_at_epoch=now + lease_seconds,
                spawn_ids=sorted(own_ids),
            )
        else:
            reservations.pop(token, None)
        waiting_ids = {item for lease in reservations.values() for item in lease.spawn_ids}
        observed_ids.update(observed_add_ids)
        return (
            SpawnObservations(
                v=1,
                spawn_id=parent_spawn_id,
                updated_at_ms=time.time() * 1000,
                observed_spawn_ids=sorted(observed_ids),
                waiting_spawn_ids=sorted(waiting_ids),
                wait_reservations=reservations,
            ),
            None,
        )

    # The file lives in the parent's spawn directory, which pruning and archiving
    # delete: write only while the published row exists, never recreate the directory.
    try:
        mutate_model(
            path,
            SpawnObservations,
            update,
            lock=published_artifact_lock(
                runtime_root,
                SpawnId(parent_spawn_id),
                timeout=lock_timeout,
            ),
            read_current=lambda: _read(path, parent_spawn_id),
            indent=2,
        )
    except (PublishedArtifactUnavailable, SpawnStateQuarantined):
        return False
    # legacy-observed-spawns (#719): A Pi session started by an older build reads only
    # this file, so mirror updates wherever Pi already keeps the legacy directory.
    legacy_dir = _legacy_dir(runtime_root, parent_spawn_id)
    if legacy_dir.is_dir():
        legacy_path = legacy_dir / "observed-spawns.json"
        mutate_model(
            legacy_path,
            SpawnObservations,
            update,
            lock=lock_file(legacy_dir / "observed-spawns.lock", timeout=lock_timeout),
            read_current=lambda: _read(legacy_path, parent_spawn_id),
            indent=2,
        )
    return True


def consumed_ids(runtime_root: Path, parent_ids: Iterable[str]) -> frozenset[str]:
    """Return durable observations across every supplied primary generation."""

    consumed: set[str] = set()
    for parent_id in set(parent_ids):
        for observations in _documents(runtime_root, parent_id):
            consumed.update(observations.observed_spawn_ids)
    return frozenset(consumed)


def leased_ids(runtime_root: Path, parent_ids: Iterable[str]) -> frozenset[str]:
    """Return results held by an unexpired, birth-verified explicit wait."""

    now = time.time()
    leased: set[str] = set()
    for parent_id in set(parent_ids):
        for observations in _documents(runtime_root, parent_id):
            for reservation in observations.wait_reservations.values():
                if reservation.expires_at_epoch <= now:
                    continue
                if not is_process_alive_with_birth(
                    reservation.owner_pid,
                    reservation.owner_birth_epoch,
                ):
                    continue
                leased.update(reservation.spawn_ids)
    return frozenset(leased)


__all__ = [
    "SpawnObservations",
    "consumed_ids",
    "leased_ids",
    "read_legacy",  # legacy-observed-spawns (#719)
    "record",
]
