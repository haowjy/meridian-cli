"""Crash-safe consumption, wait-lease and wait-pin state for spawn results.

A result is one launch of a spawn, so every entry is keyed ``"<spawn id>:<n>"``
(:func:`launch_key`). ``observed-launches.json`` is the only file written. The files older
builds wrote (``observed-spawns.json``, keyed by bare spawn ID) are read and never written:
a bare ID is launch 1, which is exact because an older build cannot read a v4 row and so only
ever consumed launch 1.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

import psutil
from pydantic import Field, StrictInt

from meridian.lib.core.types import SpawnId
from meridian.lib.state.json_doc import VersionedPrivateDoc, mutate_model, read_model
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.paths import observed_launches_path, observed_spawns_path
from meridian.lib.state.spawn.repository import SpawnStateQuarantined
from meridian.lib.state.spawn_aggregate import (
    PublishedArtifactUnavailable,
    published_artifact_lock,
)


def launch_key(spawn_id: str, n: int) -> str:
    """The ledger key of one launch."""
    return f"{spawn_id}:{n}"


class WaitLease(VersionedPrivateDoc):
    owner_pid: StrictInt = Field(gt=0)
    owner_birth_epoch: float = Field(gt=0)
    expires_at_epoch: float
    launches: list[str]


class WaitPin(VersionedPrivateDoc):
    """The launch a caller's ``spawn wait`` waits on, kept across ``--yield-after-secs`` re-runs."""

    n: StrictInt = Field(ge=1)
    expires_at: float


class LaunchObservations(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float = 0.0
    observed_launches: list[str]
    waiting_launches: list[str] = Field(default_factory=list)
    wait_reservations: dict[str, WaitLease] = Field(default_factory=dict)
    wait_pins: dict[str, WaitPin] = Field(default_factory=dict)


class LegacyLease(VersionedPrivateDoc):
    owner_pid: StrictInt = Field(gt=0)
    owner_birth_epoch: float = Field(gt=0)
    expires_at_epoch: float
    spawn_ids: list[str]


class LegacyObservations(VersionedPrivateDoc):
    """``observed-spawns.json`` as older builds wrote it. Read-only."""

    v: Literal[1]
    spawn_id: str
    updated_at_ms: float = 0.0
    observed_spawn_ids: list[str]
    waiting_spawn_ids: list[str] = Field(default_factory=list)
    wait_reservations: dict[str, LegacyLease] = Field(default_factory=dict)


def _read(path: Path, parent_spawn_id: str) -> LaunchObservations | None:
    observations = read_model(path, LaunchObservations)
    if observations is None or observations.spawn_id != parent_spawn_id:
        return None
    return observations


def _read_legacy(runtime_root: Path, parent_spawn_id: str) -> LegacyObservations | None:
    legacy = read_model(observed_spawns_path(runtime_root, parent_spawn_id), LegacyObservations)
    if legacy is None or legacy.spawn_id != parent_spawn_id:
        return None
    return legacy


def record(
    *,
    runtime_root: Path,
    parent_spawn_id: str | None,
    waiting_add: Iterable[str] = (),
    waiting_remove: Iterable[str] = (),
    observed_add: Iterable[str] = (),
    pins_set: Mapping[str, WaitPin] | None = None,
    pins_clear: Iterable[str] = (),
    lease_seconds: float = 30.0,
    lock_timeout: float | None = None,
) -> bool:
    """Record results consumed by a parent, leased by its explicit wait, or pinned by it.

    Launches are :func:`launch_key` strings. Returns ``False`` when the parent's published
    row is gone or unreadable, so nothing was written. ``spawn ack`` treats that as an
    error; ``spawn wait`` proceeds without a record because no reader selects that parent's
    children. A finite ``lock_timeout`` bounds best-effort shown-result observation.

    A pin dies with the first of: its launch being consumed, expiry, ``pins_clear``.
    """

    waiting_add_ids = tuple(waiting_add)
    waiting_remove_ids = tuple(waiting_remove)
    observed_add_ids = tuple(observed_add)
    pin_updates = dict(pins_set or {})
    pins_clear_ids = tuple(pins_clear)
    if not parent_spawn_id or not (
        waiting_add_ids or waiting_remove_ids or observed_add_ids or pin_updates or pins_clear_ids
    ):
        return True

    path = observed_launches_path(runtime_root, parent_spawn_id)

    def update(existing: LaunchObservations | None) -> tuple[LaunchObservations, None]:
        existing = existing or LaunchObservations(
            v=1,
            spawn_id=parent_spawn_id,
            observed_launches=[],
        )
        observed = set(existing.observed_launches)
        now = time.time()
        reservations = {
            token: lease
            for token, lease in existing.wait_reservations.items()
            if lease.expires_at_epoch > now
        }
        token = f"{os.getpid()}:{threading.get_ident()}"
        own_lease = reservations.get(token)
        own = set(own_lease.launches if own_lease else ())
        own.update(waiting_add_ids)
        own.difference_update(waiting_remove_ids)
        if own:
            reservations[token] = WaitLease(
                owner_pid=os.getpid(),
                owner_birth_epoch=psutil.Process().create_time(),
                expires_at_epoch=now + lease_seconds,
                launches=sorted(own),
            )
        else:
            reservations.pop(token, None)
        waiting = {item for lease in reservations.values() for item in lease.launches}
        observed.update(observed_add_ids)
        pins = {**existing.wait_pins, **pin_updates}
        for target in pins_clear_ids:
            pins.pop(target, None)
        pins = {
            target: pin
            for target, pin in pins.items()
            if pin.expires_at > now and launch_key(target, pin.n) not in observed
        }
        return (
            LaunchObservations(
                v=1,
                spawn_id=parent_spawn_id,
                updated_at_ms=time.time() * 1000,
                observed_launches=sorted(observed),
                waiting_launches=sorted(waiting),
                wait_reservations=reservations,
                wait_pins=pins,
            ),
            None,
        )

    # The file lives in the parent's spawn directory, which pruning and archiving
    # delete: write only while the published row exists, never recreate the directory.
    try:
        mutate_model(
            path,
            LaunchObservations,
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
    return True


def clear_wait_pin(runtime_root: Path, parent_spawn_id: str, target_spawn_id: str) -> bool:
    """Drop a caller's pin on a target: a continue moved the caller past that launch."""
    return record(
        runtime_root=runtime_root,
        parent_spawn_id=parent_spawn_id,
        pins_clear=(target_spawn_id,),
    )


def live_wait_pins(
    runtime_root: Path, parent_spawn_id: str, consumed: Iterable[str]
) -> dict[str, int]:
    """The caller's unexpired pins whose launch is not in ``consumed`` (target -> launch)."""

    observations = _read(observed_launches_path(runtime_root, parent_spawn_id), parent_spawn_id)
    if observations is None:
        return {}
    taken = frozenset(consumed)
    now = time.time()
    return {
        target: pin.n
        for target, pin in observations.wait_pins.items()
        if pin.expires_at > now and launch_key(target, pin.n) not in taken
    }


def leased_launches(runtime_root: Path, parent_ids: Iterable[str]) -> frozenset[str]:
    """Launches held by an unexpired, birth-verified explicit wait."""

    now = time.time()
    leased: set[str] = set()
    for parent_id in set(parent_ids):
        current = _read(observed_launches_path(runtime_root, parent_id), parent_id)
        if current is not None:
            for lease in current.wait_reservations.values():
                if _lease_live(
                    lease.expires_at_epoch, lease.owner_pid, lease.owner_birth_epoch, now
                ):
                    leased.update(lease.launches)
        legacy = _read_legacy(runtime_root, parent_id)
        if legacy is not None:
            for old in legacy.wait_reservations.values():
                if _lease_live(old.expires_at_epoch, old.owner_pid, old.owner_birth_epoch, now):
                    leased.update(launch_key(spawn_id, 1) for spawn_id in old.spawn_ids)
    return frozenset(leased)


def _lease_live(expires_at: float, pid: int, birth_epoch: float, now: float) -> bool:
    return expires_at > now and is_process_alive_with_birth(pid, birth_epoch)


__all__ = [
    "LaunchObservations",
    "LegacyObservations",
    "WaitLease",
    "WaitPin",
    "clear_wait_pin",
    "launch_key",
    "leased_launches",
    "live_wait_pins",
    "record",
]
