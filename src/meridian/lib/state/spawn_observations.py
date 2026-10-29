"""Crash-safe consumption, wait-lease and wait-pin state for spawn results.

A result is one launch of a spawn, so every entry is keyed ``"<spawn id>:<n>"``
(``str(LaunchKey)``; the string form exists only in these files). ``observed-launches.json``
is the only file written. The files older
builds wrote (``observed-spawns.json``, keyed by bare spawn ID) are read and never written:
a bare ID is launch 1, which is exact because an older build cannot read a v4 row and so only
ever consumed launch 1.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Container, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

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

if TYPE_CHECKING:
    from meridian.lib.state.spawn.model import SpawnRecord


class LaunchKey(NamedTuple):
    """One launch of a spawn: what is owed, leased, pinned and consumed.

    ``str()`` is the ledger key, the only place the string form is used.
    """

    spawn_id: str
    n: int

    @classmethod
    def of(cls, row: SpawnRecord) -> LaunchKey:
        return cls(row.id, row.launch_number)

    def __str__(self) -> str:
        return f"{self.spawn_id}:{self.n}"


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
    # What the last ``spawn collect`` delivered per spawn, so ``spawn ack`` consumes exactly
    # those launches and not one that finished or started after the delivery.
    delivered: dict[str, list[int]] = Field(default_factory=dict)


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


def legacy_view(legacy: LegacyObservations) -> LaunchObservations:
    """An older build's ledger as launch-1 entries: the one place a bare ID gains a launch."""
    return LaunchObservations(
        v=1,
        spawn_id=legacy.spawn_id,
        updated_at_ms=legacy.updated_at_ms,
        observed_launches=[str(LaunchKey(item, 1)) for item in legacy.observed_spawn_ids],
        waiting_launches=[str(LaunchKey(item, 1)) for item in legacy.waiting_spawn_ids],
        wait_reservations={
            token: WaitLease(
                owner_pid=lease.owner_pid,
                owner_birth_epoch=lease.owner_birth_epoch,
                expires_at_epoch=lease.expires_at_epoch,
                launches=[str(LaunchKey(item, 1)) for item in lease.spawn_ids],
            )
            for token, lease in legacy.wait_reservations.items()
        },
    )


def read_legacy(runtime_root: Path, parent_spawn_id: str) -> LaunchObservations | None:
    legacy = read_model(observed_spawns_path(runtime_root, parent_spawn_id), LegacyObservations)
    if legacy is None or legacy.spawn_id != parent_spawn_id:
        return None
    return legacy_view(legacy)


def _pin_live(pin: WaitPin, target: str, consumed: Container[str], now: float) -> bool:
    return pin.expires_at > now and str(LaunchKey(target, pin.n)) not in consumed


def record(
    *,
    runtime_root: Path,
    parent_spawn_id: str | None,
    waiting_add: Iterable[str] = (),
    waiting_remove: Iterable[str] = (),
    observed_add: Iterable[str] = (),
    pins_set: Mapping[str, WaitPin] | None = None,
    pins_clear: Iterable[str] = (),
    delivered_set: Mapping[str, list[int]] | None = None,
    ack_delivered: Iterable[str] = (),
    lease_seconds: float = 30.0,
    lock_timeout: float | None = None,
) -> bool:
    """Record results consumed by a parent, leased by its explicit wait, or pinned by it.

    Launches are :func:`launch_key` strings. Returns ``False`` when the parent's published
    row is gone or unreadable, so nothing was written. ``spawn ack`` treats that as an
    error; ``spawn wait`` proceeds without a record because no reader selects that parent's
    children. A finite ``lock_timeout`` bounds best-effort shown-result observation.

    A pin dies with the first of: its launch being consumed, expiry, ``pins_clear``.

    ``delivered_set`` replaces the delivery record of each spawn it names. ``ack_delivered``
    consumes the recorded launches of those spawns and drops their records; a spawn with no
    record is left to the caller. Only ``ack_delivered`` drops a record, so a record that is
    absent means "delivered and acked", never "consumed some other way and maybe not delivered".
    """

    waiting_add_ids = tuple(waiting_add)
    waiting_remove_ids = tuple(waiting_remove)
    observed_add_ids = tuple(observed_add)
    pin_updates = dict(pins_set or {})
    pins_clear_ids = tuple(pins_clear)
    delivery_updates = dict(delivered_set or {})
    ack_ids = tuple(ack_delivered)
    if not parent_spawn_id or not (
        waiting_add_ids
        or waiting_remove_ids
        or observed_add_ids
        or pin_updates
        or pins_clear_ids
        or delivery_updates
        or ack_ids
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
        delivered = {**existing.delivered, **delivery_updates}
        for target in ack_ids:
            observed.update(str(LaunchKey(target, k)) for k in delivered.pop(target, ()))
        pins = {**existing.wait_pins, **pin_updates}
        for target in pins_clear_ids:
            pins.pop(target, None)
        pins = {
            target: pin for target, pin in pins.items() if _pin_live(pin, target, observed, now)
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
                delivered=delivered,
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
        if _pin_live(pin, target, taken, now)
    }


def leased_launches(runtime_root: Path, parent_ids: Iterable[str]) -> frozenset[str]:
    """Launches held by an unexpired, birth-verified explicit wait."""

    now = time.time()
    leased: set[str] = set()
    for parent_id in set(parent_ids):
        for doc in (
            _read(observed_launches_path(runtime_root, parent_id), parent_id),
            read_legacy(runtime_root, parent_id),
        ):
            if doc is None:
                continue
            for lease in doc.wait_reservations.values():
                if _lease_live(
                    lease.expires_at_epoch, lease.owner_pid, lease.owner_birth_epoch, now
                ):
                    leased.update(lease.launches)
    return frozenset(leased)


def _lease_live(expires_at: float, pid: int, birth_epoch: float, now: float) -> bool:
    return expires_at > now and is_process_alive_with_birth(pid, birth_epoch)


def delivered_launches(runtime_root: Path, parent_spawn_id: str) -> dict[str, list[int]]:
    """What the parent's last ``spawn collect`` delivered and ``spawn ack`` has not taken."""
    observations = _read(observed_launches_path(runtime_root, parent_spawn_id), parent_spawn_id)
    return dict(observations.delivered) if observations is not None else {}


__all__ = [
    "LaunchKey",
    "LaunchObservations",
    "LegacyObservations",
    "WaitLease",
    "WaitPin",
    "clear_wait_pin",
    "delivered_launches",
    "leased_launches",
    "legacy_view",
    "live_wait_pins",
    "read_legacy",
    "record",
]
