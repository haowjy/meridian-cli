"""Rewrite spawn rows older builds wrote in the format this build writes; once per root.

Three old shapes are rewritten: v2 rows (before 0.3.43), v3 rows with the dogfood
flat boundary fields, and finished rows without ``launch.lock`` (mostly from before
0.10.18). Readers of all three stay, for rows this pass has not reached yet.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.platform.atomic import PRIVATE_FILE_MODE, atomic_write_text
from meridian.lib.platform.locking import FileLockTimeout, lock_file
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.launch_lock import lock_path
from meridian.lib.state.spawn.legacy import upgrade_legacy_spawn_state
from meridian.lib.state.spawn.model import LegacyRunBoundaryOutcome, RunBoundaryOutcome
from meridian.lib.state.spawn.repository import (
    DOGFOOD_BOUNDARY_FIELDS,
    MAX_STATE_VERSION,
    StoredSpawnState,
    iter_spawn_ids,
    spawn_lock_path,
    stored_state_to_record,
)
from meridian.lib.state.spawn_status import legacy_runner_alive

MARKER = "legacy-spawn-rows-v1.json"

type RowKind = Literal["v2", "dogfood", "lockless"]

_DOGFOOD_MARKERS = tuple(f'"{name}"'.encode() for name in DOGFOOD_BOUNDARY_FIELDS)


def _translate_dogfood(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw)
    entry_chat_id = data.pop("entry_chat_id", None)
    exit_chat_id = data.pop("exit_chat_id", None)
    exit_identity = data.pop("exit_identity", None)
    trampoline = data.pop("trampoline_successor_id", None)
    if data.get("chat_id") is None and entry_chat_id is not None:
        data["chat_id"] = entry_chat_id
    if exit_identity is not None or exit_chat_id is not None:
        legacy = LegacyRunBoundaryOutcome.model_validate(
            {
                "status": exit_identity if exit_identity is not None else "unresolved",
                "exit_chat_id": exit_chat_id,
            }
        )
        if data.get("run_boundary") is None:
            data["run_boundary"] = legacy.model_dump(exclude={"exit_chat_id"})
        if legacy.exit_chat_id is not None and data.get("current_chat_id") is None:
            data["current_chat_id"] = legacy.exit_chat_id
    if trampoline is not None:
        boundary = RunBoundaryOutcome.model_validate(
            data.get("run_boundary") or {"status": "unresolved"}
        ).model_dump()
        data["run_boundary"] = {**boundary, "trampoline_successor_id": trampoline}
    return data


type Change = Literal["v2 to v3", "dogfood fields", "launch lock"]


@dataclass(frozen=True)
class LegacyRow:
    spawn_id: str
    kinds: tuple[RowKind, ...]
    changed: tuple[Change, ...] = ()
    # Why the row failed; empty otherwise.
    reason: str = ""

    @property
    def dogfood(self) -> bool:
        return "dogfood" in self.kinds


@dataclass(frozen=True)
class LegacyRowRepair:
    rewritten: tuple[LegacyRow, ...]
    """Rows this pass changed, including deferred or unsettled ones whose content it rewrote."""
    failed: tuple[LegacyRow, ...]
    """Rows this build cannot read; they stay as they are, and doctor reports them."""
    deferred: tuple[LegacyRow, ...]
    """Rows whose old-build runner may still be alive; a later pass finishes them."""
    unsettled: tuple[LegacyRow, ...] = ()
    """Active rows without a launch lock whose runner is gone. They keep no lock: a free
    lock would let the crash repair and ``doctor --kill-orphans`` persist an outcome on
    PID evidence alone, which they refuse for these rows. A cancel or continue that names
    one settles it, and a later doctor run finishes it."""
    index_rearm_warning: str | None = None

    @property
    def done(self) -> bool:
        """No later pass of this build would change anything: only a live runner waits."""
        return not self.deferred


def _kinds(raw: object, has_launch_lock: bool) -> tuple[RowKind, ...]:
    """The old shapes a parsed row has; empty for a current row or one that is not ours.

    A newer build's row (``v`` above ``MAX_STATE_VERSION``) and a restored historical
    row (inert; its archive digest covers the stored bytes) are never ours.
    """
    if not isinstance(raw, dict):
        return ()
    row = cast("dict[str, Any]", raw)
    version = row.get("v", 2)
    if type(version) is not int or version > MAX_STATE_VERSION:
        return ()
    if row.get("record_mode") == "historical":
        return ()
    kinds: list[RowKind] = []
    if version == 2:
        kinds.append("v2")
    if row.keys() & set(DOGFOOD_BOUNDARY_FIELDS):
        kinds.append("dogfood")
    if not has_launch_lock:
        kinds.append("lockless")
    return tuple(kinds)


def _read_kinds(state_path: Path, launch_lock: Path) -> tuple[RowKind, ...]:
    """Unlocked preview, so a current row costs one read and no lock."""
    content = state_path.read_bytes()
    try:
        raw: object = json.loads(content)
    except ValueError:
        # Damaged JSON is only ours when it is a dogfood row; the locked read reports it.
        return ("dogfood",) if any(marker in content for marker in _DOGFOOD_MARKERS) else ()
    return _kinds(raw, launch_lock.exists())


type _Outcome = Literal["rewritten", "deferred", "unsettled"]

_CONTENT_CHANGES: tuple[tuple[RowKind, Change], ...] = (
    ("v2", "v2 to v3"),
    ("dogfood", "dogfood fields"),
)


def _repair_row(
    changes: HistoryChanges, spawns_dir: Path, spawn_id: str
) -> tuple[_Outcome, LegacyRow] | None:
    """Re-read and rewrite one row under its locks; None when it is already current."""
    state_path = spawns_dir / spawn_id / "state.json"
    launch_lock = lock_path(spawns_dir.parent, spawn_id)
    with (
        lock_file(changes.mutation_lock, mode="shared"),
        lock_file(spawn_lock_path(spawns_dir, spawn_id), reentrant=False),
    ):
        try:
            raw = json.loads(state_path.read_bytes())
        except FileNotFoundError:
            return None
        kinds = _kinds(raw, launch_lock.exists())
        if not kinds:
            return None
        candidate = cast("dict[str, Any]", raw)
        if "v2" in kinds:
            candidate = upgrade_legacy_spawn_state(candidate)
        if "dogfood" in kinds:
            candidate = _translate_dogfood(candidate)
        stored = StoredSpawnState.model_validate(candidate)
        if stored.id != spawn_id:
            raise ValueError(f"state names spawn {stored.id!r}, not its directory {spawn_id!r}")
        # A live v2 runner (0.3.42 or older) rereads its row and cannot read v3, so its
        # row waits; a live dogfood runner reads the rewritten row, which keeps its fields.
        old_runner_alive = "lockless" in kinds and legacy_runner_alive(
            stored_state_to_record(stored)
        )
        changed: list[Change] = []
        if "dogfood" in kinds or ("v2" in kinds and not old_runner_alive):
            # Write the upgraded raw object, not a model dump: nested objects such as
            # launch_policy_snapshot keep keys this build ignores but older builds read.
            changes.mark(HistorySource(kind="spawn", key=spawn_id))
            atomic_write_text(
                state_path, json.dumps(candidate, indent=2) + "\n", permissions=PRIVATE_FILE_MODE
            )
            changed.extend(change for kind, change in _CONTENT_CHANGES if kind in kinds)
        if old_runner_alive:
            return "deferred", LegacyRow(spawn_id, kinds, tuple(changed))
        if "lockless" in kinds:
            if is_active_spawn_status(stored.status):
                return "unsettled", LegacyRow(spawn_id, kinds, tuple(changed))
            # Lock creators hold the spawn lock too, so the file is still missing; it is
            # never replaced.
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            os.close(os.open(launch_lock, flags, 0o600))
            changed.append("launch lock")
    return "rewritten", LegacyRow(spawn_id, kinds, tuple(changed))


def repair_legacy_spawn_rows(runtime_root: Path) -> LegacyRowRepair:
    """Rewrite old-format ``state.json`` rows in place; idempotent and safe to interrupt.

    Each row is rewritten atomically and isolated: a row that fails is reported and
    the pass moves on. The marker is written once no later pass would change anything:
    rows this build cannot read and active lockless rows stay as they are for good, so
    only a row whose old runner may be alive holds it back.
    """
    spawns_dir = runtime_root / "spawns"
    changes = HistoryChanges(runtime_root)
    rewritten: list[LegacyRow] = []
    failed: list[LegacyRow] = []
    deferred: list[LegacyRow] = []
    unsettled: list[LegacyRow] = []
    left = {"deferred": deferred, "unsettled": unsettled}
    for spawn_id in iter_spawn_ids(spawns_dir):
        kinds: tuple[RowKind, ...] = ()
        try:
            kinds = _read_kinds(
                spawns_dir / spawn_id / "state.json", lock_path(runtime_root, spawn_id)
            )
            outcome = _repair_row(changes, spawns_dir, spawn_id) if kinds else None
        except FileNotFoundError:
            continue
        except Exception as exc:
            failed.append(LegacyRow(spawn_id, kinds, reason=_reason(exc)))
            continue
        if outcome is not None:
            bucket, row = outcome
            if bucket in left:
                left[bucket].append(row)
            if row.changed:
                rewritten.append(row)
    index_rearm_warning = None
    # Dogfood rows quarantined before the rewrite, which may have latched index failure.
    if any(row.dogfood for row in rewritten):
        try:
            warnings = HistoryIndex(runtime_root).clear_authority_failure()
            if warnings:
                index_rearm_warning = "; ".join(warnings)
        except FileLockTimeout as exc:
            index_rearm_warning = f"Could not re-arm history index initialization: {exc}"
    result = LegacyRowRepair(
        rewritten=_sorted(rewritten),
        failed=_sorted(failed),
        deferred=_sorted(deferred),
        unsettled=_sorted(unsettled),
        index_rearm_warning=index_rearm_warning,
    )
    marker = runtime_root / MARKER
    if result.done and not marker.exists():
        atomic_write_text(
            marker,
            json.dumps({"v": 1, "completed_at": utc_now_iso()}) + "\n",
            permissions=PRIVATE_FILE_MODE,
        )
    return result


def _sorted(rows: Iterable[LegacyRow]) -> tuple[LegacyRow, ...]:
    return tuple(sorted(rows, key=lambda row: row.spawn_id))


def _reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        detail = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors()
        )
    else:
        detail = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {detail}"


__all__ = [
    "MARKER",
    "LegacyRow",
    "LegacyRowRepair",
    "repair_legacy_spawn_rows",
]
