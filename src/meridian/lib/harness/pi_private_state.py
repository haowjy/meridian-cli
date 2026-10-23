"""Validated private Pi coordination contracts; missing files alone mean empty."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, Field, StrictBool, StrictInt

from meridian.lib.state.json_doc import VersionedPrivateDoc
from meridian.lib.state.paths import observed_spawns_path
from meridian.lib.state.spawn_observations import SpawnObservations

_Model = TypeVar("_Model", bound=BaseModel)

DELIVERY_RECEIPTS_FILENAME = "delivery-receipts.json"
OBSERVED_SPAWNS_FILENAME = "observed-spawns.json"
CLEARED_SPAWNS_FILENAME = "cleared-spawns.json"


@dataclass(frozen=True)
class PiConsumptionState:
    consumed_ids: frozenset[str]
    receipt_message_ids: frozenset[str]


def pi_coordination_dir(runtime_root: Path, spawn_id: str) -> Path:
    """Return one Pi parent's private coordination directory."""

    return runtime_root / "pi-bash" / spawn_id


def read_pi_private_doc(
    file: Path,
    model: type[_Model],
    *,
    spawn_id: str,
) -> _Model | None:
    """Read a private Pi document and validate that it belongs to its directory."""

    try:
        value = model.model_validate_json(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if getattr(value, "spawn_id", None) != spawn_id:
        raise ValueError("private-file parent identity mismatch")
    return value


class BashEvidence(VersionedPrivateDoc):
    bash_id: str
    command: str
    cwd: str
    pid: StrictInt | None = Field(ge=1)
    status: Literal["running", "exited", "killed", "timed_out"]
    is_background: StrictBool
    is_tracked: StrictBool
    exit_code: StrictInt | None
    started_at_ms: float
    ended_at_ms: float | None
    log_path: str
    stdout_log_path: str
    stderr_log_path: str
    log_bytes: StrictInt = Field(ge=0)
    timeout_min: float
    originating_bash_id: str | None
    notification_consumed_at_ms: float | None = None
    execution_error: str = ""
    ping_sent_at_ms: float | None = None


class BashEvidenceFile(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float
    records: dict[str, BashEvidence]
    runtime_error: str = ""


class DeliveryReceipts(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    messages: dict[str, list[str]]


class DeliveryObservations(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    observed_message_ids: list[str]


class DeliveryFault(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    operation: Literal["scan", "admission"] | None
    error: str | None


class ClearedSpawns(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float
    cleared_spawn_ids: list[str]


def read_pi_consumption_state(
    runtime_root: Path,
    parent_spawn_id: str,
    *,
    on_read: Callable[[Path, str | None], None] | None = None,
) -> PiConsumptionState:
    """Read every result-consumption source for one Pi parent once.

    A damaged source is skipped independently. ``on_read`` receives its diagnostic,
    or ``None`` after a successful or missing-file read.
    """

    coordination_dir = pi_coordination_dir(runtime_root, parent_spawn_id)

    def read(file: Path, model: type[_Model]) -> _Model | None:
        try:
            value = read_pi_private_doc(file, model, spawn_id=parent_spawn_id)
        except (OSError, UnicodeError, ValueError) as exc:
            if on_read is not None:
                on_read(file, str(exc))
            return None
        if on_read is not None:
            on_read(file, None)
        return value

    receipts = read(
        coordination_dir / DELIVERY_RECEIPTS_FILENAME,
        DeliveryReceipts,
    )
    current_observations = read(
        observed_spawns_path(runtime_root, parent_spawn_id),
        SpawnObservations,
    )
    legacy_observations = read(
        coordination_dir / OBSERVED_SPAWNS_FILENAME,
        SpawnObservations,
    )
    cleared = read(
        coordination_dir / CLEARED_SPAWNS_FILENAME,
        ClearedSpawns,
    )

    consumed: set[str] = set()
    receipt_message_ids: set[str] = set()
    if isinstance(receipts, DeliveryReceipts):
        receipt_message_ids.update(receipts.messages)
        consumed.update(work_id for ids in receipts.messages.values() for work_id in ids)
    for observations in (current_observations, legacy_observations):
        if isinstance(observations, SpawnObservations):
            consumed.update(observations.observed_spawn_ids)
    if isinstance(cleared, ClearedSpawns):
        consumed.update(cleared.cleared_spawn_ids)
    return PiConsumptionState(
        consumed_ids=frozenset(consumed),
        receipt_message_ids=frozenset(receipt_message_ids),
    )
