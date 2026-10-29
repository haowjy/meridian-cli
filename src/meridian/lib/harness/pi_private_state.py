"""Validated private Pi coordination contracts; missing files alone mean empty."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, Field, StrictBool, StrictInt

from meridian.lib.state.json_doc import VersionedPrivateDoc
from meridian.lib.state.paths import observed_launches_path
from meridian.lib.state.spawn_observations import (
    LaunchKey,
    LaunchObservations,
    read_legacy,
)

_Model = TypeVar("_Model", bound=BaseModel)

# Entries are work IDs: ``"<spawn id>:<n>"`` for a launch, a bare ID for a Pi bash task.
# The files older builds wrote (below) key spawns by bare ID and are read, never written.
# A pre-launch ledger entry names launch 1 (older builds cannot read a v4 row), and the
# Pi mirror of ``observed-spawns.json`` they also wrote is no longer read.
DELIVERY_RECEIPTS_FILENAME = "launch-receipts.json"
CLEARED_LAUNCHES_FILENAME = "cleared-launches.json"
OBSERVED_LAUNCHES_FILENAME = "observed-launches.json"
LEGACY_DELIVERY_RECEIPTS_FILENAME = "delivery-receipts.json"
LEGACY_OBSERVED_SPAWNS_FILENAME = "observed-spawns.json"
LEGACY_CLEARED_SPAWNS_FILENAME = "cleared-spawns.json"


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


class ClearedLaunches(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float
    cleared_launches: list[str]


class LegacyClearedSpawns(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float
    cleared_spawn_ids: list[str]


def _legacy_entries(ids: Iterable[str]) -> Iterator[str]:
    """A pre-launch work ID: a bash task as written, a spawn as its launch 1."""
    for work_id in ids:
        yield work_id
        yield str(LaunchKey(work_id, 1))


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

    receipts = read(coordination_dir / DELIVERY_RECEIPTS_FILENAME, DeliveryReceipts)
    legacy_receipts = read(coordination_dir / LEGACY_DELIVERY_RECEIPTS_FILENAME, DeliveryReceipts)
    observations = read(observed_launches_path(runtime_root, parent_spawn_id), LaunchObservations)
    legacy_observations = read_legacy(runtime_root, parent_spawn_id)
    cleared = read(coordination_dir / CLEARED_LAUNCHES_FILENAME, ClearedLaunches)
    legacy_cleared = read(coordination_dir / LEGACY_CLEARED_SPAWNS_FILENAME, LegacyClearedSpawns)

    consumed: set[str] = set()
    receipt_message_ids: set[str] = set()
    for document in (receipts, legacy_receipts):
        if document is not None:
            receipt_message_ids.update(document.messages)
    if receipts is not None:
        consumed.update(work_id for ids in receipts.messages.values() for work_id in ids)
    if legacy_receipts is not None:
        consumed.update(
            _legacy_entries(work_id for ids in legacy_receipts.messages.values() for work_id in ids)
        )
    if observations is not None:
        consumed.update(observations.observed_launches)
    if legacy_observations is not None:
        consumed.update(legacy_observations.observed_launches)
    if cleared is not None:
        consumed.update(cleared.cleared_launches)
    if legacy_cleared is not None:
        consumed.update(_legacy_entries(legacy_cleared.cleared_spawn_ids))
    return PiConsumptionState(
        consumed_ids=frozenset(consumed),
        receipt_message_ids=frozenset(receipt_message_ids),
    )
