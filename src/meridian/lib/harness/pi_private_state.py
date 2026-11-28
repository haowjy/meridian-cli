"""Validated private Pi coordination contracts; missing files alone mean empty."""

from __future__ import annotations

import os
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

import psutil
import structlog
from pydantic import BaseModel, Field, StrictBool, StrictInt

from meridian.lib.harness.command_ports import CommandPorts
from meridian.lib.state.json_doc import VersionedPrivateDoc, read_owned_doc, write_model
from meridian.lib.state.spawn_observations import LaunchKey, observed_launches

logger = structlog.get_logger()

# Set by managed bash on each task's shell: the directory this task's hold files go in.
PI_BASH_HOLD_DIR_ENV = "_MERIDIAN_PI_BASH_HOLD_DIR"

_Model = TypeVar("_Model", bound=BaseModel)

# Entries are work IDs: ``"<spawn id>:<n>"`` for a launch, a bare ID for a Pi bash task.
# The files older builds wrote (below) key spawns by bare ID and are read, never written.
# A pre-launch ledger entry names launch 1 (older builds cannot read a v4 row), and the
# Pi mirror of ``observed-spawns.json`` is not read.
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

    return runtime_root / "pi-bash" / spawn_id


class PiBashHold(VersionedPrivateDoc):
    """A Meridian wait or foreground spawn is running inside one managed-bash task."""

    v: Literal[1]
    owner_pid: StrictInt = Field(gt=0)
    owner_birth_epoch: float = Field(gt=0)


@contextmanager
def hold_pi_bash() -> Generator[None]:
    """Mark the calling Pi bash task as running this command until the body returns.

    Managed bash names the task's hold directory in ``_MERIDIAN_PI_BASH_HOLD_DIR``; this
    writes ``<pid>.json`` there once and removes it on exit. The extension treats the task
    as held while any file's owner is alive, so a crash needs no cleanup. Outside a Pi bash
    task the variable is unset and nothing is written.
    """

    hold_dir = os.environ.get(PI_BASH_HOLD_DIR_ENV)
    if not hold_dir:
        yield
        return
    pid = os.getpid()
    path = Path(hold_dir) / f"{pid}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_model(
            path,
            PiBashHold(v=1, owner_pid=pid, owner_birth_epoch=psutil.Process().create_time()),
        )
    except OSError as exc:
        logger.warning("Pi bash hold not written; the task may be backgrounded", error=str(exc))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


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
    originating_bash_id: str | None = None  # older builds wrote it; ignored
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


def read_pi_delivery_consumption(
    runtime_root: Path,
    parent_spawn_id: str,
    *,
    on_read: Callable[[Path, str | None], None] | None = None,
) -> PiConsumptionState:
    """What Pi's spawn-watch delivery consumed for one parent: its receipts and cleared
    launches, and the files older builds wrote for them. A damaged source is skipped."""

    coordination_dir = pi_coordination_dir(runtime_root, parent_spawn_id)

    def read(file: Path, model: type[_Model]) -> _Model | None:
        return read_owned_doc(file, model, owner=parent_spawn_id, on_read=on_read)

    receipts = read(coordination_dir / DELIVERY_RECEIPTS_FILENAME, DeliveryReceipts)
    legacy_receipts = read(coordination_dir / LEGACY_DELIVERY_RECEIPTS_FILENAME, DeliveryReceipts)
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
    if cleared is not None:
        consumed.update(cleared.cleared_launches)
    if legacy_cleared is not None:
        consumed.update(_legacy_entries(legacy_cleared.cleared_spawn_ids))
    return PiConsumptionState(
        consumed_ids=frozenset(consumed),
        receipt_message_ids=frozenset(receipt_message_ids),
    )


def read_pi_consumption_state(
    runtime_root: Path,
    parent_spawn_id: str,
    *,
    on_read: Callable[[Path, str | None], None] | None = None,
) -> PiConsumptionState:
    """Every result-consumption source for one Pi parent, each read once: Pi's own and
    the shared ``observed-launches.json``. A damaged source is skipped independently;
    ``on_read`` receives its diagnostic, or ``None`` after a successful or missing-file read.
    """

    own = read_pi_delivery_consumption(runtime_root, parent_spawn_id, on_read=on_read)
    observed = observed_launches(runtime_root, parent_spawn_id, on_read=on_read)
    return PiConsumptionState(own.consumed_ids | observed, own.receipt_message_ids)


def _consumed_launches(
    runtime_root: Path, parent_spawn_id: str, on_read: Callable[[Path, str | None], None] | None
) -> frozenset[str]:
    return read_pi_delivery_consumption(runtime_root, parent_spawn_id, on_read=on_read).consumed_ids


COMMAND_PORTS = CommandPorts(
    consumed_launches=_consumed_launches, blocking_command_hold=hold_pi_bash
)
