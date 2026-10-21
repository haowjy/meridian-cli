"""Crash-safe consumption and wait-lease state for spawn results."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.json_doc import read_model, write_model
from meridian.lib.state.liveness import is_process_alive_with_birth


class _ObservationModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", allow_inf_nan=False, strict=True)

    @field_validator("v", mode="before", check_fields=False)
    @classmethod
    def _exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("spawn-observation version must be integer 1")
        return value


class WaitReservation(_ObservationModel):
    owner_pid: StrictInt = Field(gt=0)
    owner_birth_epoch: float = Field(gt=0)
    expires_at_epoch: float
    spawn_ids: list[str]


class SpawnObservations(_ObservationModel):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float = 0.0
    observed_spawn_ids: list[str]
    waiting_spawn_ids: list[str] = Field(default_factory=list)
    wait_reservations: dict[str, WaitReservation] = Field(default_factory=dict)


def observation_path(runtime_root: Path, parent_spawn_id: str) -> Path:
    return runtime_root / "pi-bash" / parent_spawn_id / "observed-spawns.json"


def observation_lock_path(runtime_root: Path, parent_spawn_id: str) -> Path:
    return runtime_root / "pi-bash" / parent_spawn_id / "observed-spawns.lock"


def _read(runtime_root: Path, parent_spawn_id: str) -> SpawnObservations | None:
    observations = read_model(observation_path(runtime_root, parent_spawn_id), SpawnObservations)
    if observations is None or observations.spawn_id != parent_spawn_id:
        return None
    return observations


def record(
    *,
    runtime_root: Path,
    parent_spawn_id: str | None,
    waiting_add: Iterable[str] = (),
    waiting_remove: Iterable[str] = (),
    observed_add: Iterable[str] = (),
    lease_seconds: float = 30.0,
) -> None:
    """Record results consumed by a parent or leased by its explicit wait."""

    waiting_add_ids = tuple(waiting_add)
    waiting_remove_ids = tuple(waiting_remove)
    observed_add_ids = tuple(observed_add)
    if not parent_spawn_id or not (
        waiting_add_ids or waiting_remove_ids or observed_add_ids
    ):
        return

    path = observation_path(runtime_root, parent_spawn_id)
    with lock_file(observation_lock_path(runtime_root, parent_spawn_id)):
        existing = _read(runtime_root, parent_spawn_id) or SpawnObservations(
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
        write_model(
            path,
            SpawnObservations(
                v=1,
                spawn_id=parent_spawn_id,
                updated_at_ms=time.time() * 1000,
                observed_spawn_ids=sorted(observed_ids),
                waiting_spawn_ids=sorted(waiting_ids),
                wait_reservations=reservations,
            ),
            indent=2,
        )


def consumed_ids(runtime_root: Path, parent_ids: Iterable[str]) -> frozenset[str]:
    """Return durable observations across every supplied primary generation."""

    consumed: set[str] = set()
    for parent_id in set(parent_ids):
        observations = _read(runtime_root, parent_id)
        if observations is not None:
            consumed.update(observations.observed_spawn_ids)
    return frozenset(consumed)


def leased_ids(runtime_root: Path, parent_ids: Iterable[str]) -> frozenset[str]:
    """Return results held by an unexpired, birth-verified explicit wait."""

    now = time.time()
    leased: set[str] = set()
    for parent_id in set(parent_ids):
        observations = _read(runtime_root, parent_id)
        if observations is None:
            continue
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
    "WaitReservation",
    "consumed_ids",
    "leased_ids",
    "observation_lock_path",
    "observation_path",
    "record",
]
