"""Drop runner event streams that an exact native transcript makes redundant.

Approved archive rule: a terminal spawn older than N days whose exact native
source(s) resolve now loses its runner ``history.jsonl`` and checkpoint. Any
doubt skips. Explicit only; automatic maintenance never calls this.
"""

from __future__ import annotations

import re
import stat
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.core.types import SpawnId
from meridian.lib.ops.session_target import resolve_run_sources
from meridian.lib.platform.atomic import iter_atomic_temp_paths
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import spawn_store
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.process_scope_projection import read_scope_projection
from meridian.lib.state.reaper import scope_liveness
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact
from meridian.lib.state.timestamps import iso_timestamp_to_epoch

PRUNE_AFTER_DAYS = 14
# Names the removed runner writer used; this rule owns them for cleanup.
_RUNNER_STREAM_NAMES = ("history.jsonl", "last-observed-event.json")
_ATTEMPT_DIR = re.compile(r"attempt-\d+")


class PrunedSpawn(BaseModel):
    model_config = ConfigDict(frozen=True)
    spawn_id: str
    chat_id: str | None
    bytes: int
    files: tuple[str, ...]


class RunnerHistoryPrune(BaseModel):
    model_config = ConfigDict(frozen=True)
    applied: bool
    after_days: int
    pruned: tuple[PrunedSpawn, ...] = ()
    skipped: dict[str, tuple[str, ...]] = {}
    skipped_bytes: dict[str, int] = {}
    errors: tuple[str, ...] = ()

    def format_text(self) -> str:
        verb = "Pruned" if self.applied else "Would prune"
        mode = "" if self.applied else "dry run; --apply deletes; "
        lines = [
            f"Runner history ({mode}terminal spawns older than {self.after_days} days)",
            f"{verb}: {len(self.pruned)} spawns, "
            f"{_megabytes(sum(row.bytes for row in self.pruned))}",
        ]
        lines.extend(
            f"Skipped {reason}: {len(keys)} spawns, {_megabytes(self.skipped_bytes[reason])}"
            for reason, keys in self.skipped.items()
        )
        lines.extend(
            f"{verb} {row.spawn_id} ({row.chat_id or 'no chat'}): {_megabytes(row.bytes)}"
            for row in self.pruned
        )
        lines.extend(f"Error: {error}" for error in self.errors)
        return "\n".join(lines)


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB"


def runner_stream_files(root: Path, spawn_id: str) -> tuple[Path, ...]:
    """Runner-stream files of one spawn, including legacy artifact and attempt copies."""
    files: list[Path] = []
    for base in (root / "spawns" / spawn_id, root / "artifacts" / spawn_id):
        if not base.is_dir() or base.is_symlink():
            continue
        attempts = sorted(
            child
            for child in base.iterdir()
            if _ATTEMPT_DIR.fullmatch(child.name) and child.is_dir() and not child.is_symlink()
        )
        for directory in (base, *attempts):
            for name in _RUNNER_STREAM_NAMES:
                for path in (directory / name, *iter_atomic_temp_paths(directory, name)):
                    try:
                        regular = stat.S_ISREG(path.lstat().st_mode)
                    except FileNotFoundError:
                        continue
                    if regular:
                        files.append(path)
    return tuple(files)


def _size(paths: tuple[Path, ...]) -> int:
    return sum(path.lstat().st_size for path in paths)


def _skip_reason(root: Path, row: SpawnRecord, cutoff: float) -> str | tuple[Path, ...]:
    """Return a skip reason, or the exact native paths that make the stream redundant."""
    if row.record_mode == "historical":
        return "historical"
    if row.status not in TERMINAL_SPAWN_STATUSES:
        return "running"
    finished = iso_timestamp_to_epoch(row.terminal.finished_at if row.terminal else None)
    if finished is None:
        return "no_terminal_time"
    if finished > cutoff:
        return "recent"
    if row.run_boundary is not None and row.run_boundary.status != "verified":
        return "exit_unresolved"
    scopes = read_scope_projection(root, SpawnId(row.id))
    if any(
        scope_liveness(scope)["likely_serving"]
        for scope in scopes.scopes
        if scope.release_id not in scopes.released_ids
    ):
        return "live_scope"
    try:
        sources = resolve_run_sources(row, root)
    except NativeSessionUnavailable as exc:
        return exc.reason
    except Exception:  # The rule is "any doubt skips", not "known doubts skip".
        return "error"
    return tuple(source.path for source in sources)


def prune_runner_history(
    root: Path, *, apply: bool = False, after_days: int = PRUNE_AFTER_DAYS
) -> RunnerHistoryPrune:
    if not apply:
        return _prune(root, apply=False, after_days=after_days)
    with lock_file(root / "history-archives/archive.lock"):
        result = _prune(root, apply=True, after_days=after_days)
    HistoryIndex(root).catch_up()
    return result


def _prune(root: Path, *, apply: bool, after_days: int) -> RunnerHistoryPrune:
    cutoff = time.time() - after_days * 86400
    rows = sorted(spawn_store.list_spawns(root).records, key=lambda row: (len(row.id), row.id))
    pruned: list[PrunedSpawn] = []
    skipped: dict[str, list[str]] = {}
    skipped_bytes: dict[str, int] = {}
    errors: list[str] = []
    for row in rows:
        files = runner_stream_files(root, row.id)
        if not files:
            continue
        verdict = _skip_reason(root, row, cutoff)
        if isinstance(verdict, str):
            skipped.setdefault(verdict, []).append(row.id)
            skipped_bytes[verdict] = skipped_bytes.get(verdict, 0) + _size(files)
        elif not apply:
            pruned.append(_pruned(root, row, files))
        elif not _unlink_if_unchanged(root, row, verdict, pruned):
            errors.append(f"{row.id}: record or native source changed since planning; kept")
    return RunnerHistoryPrune(
        applied=apply,
        after_days=after_days,
        pruned=tuple(pruned),
        skipped={reason: tuple(keys) for reason, keys in skipped.items()},
        skipped_bytes=skipped_bytes,
        errors=tuple(errors),
    )


def _unlink_if_unchanged(
    root: Path, row: SpawnRecord, natives: tuple[Path, ...], pruned: list[PrunedSpawn]
) -> bool:
    """Unlink under the spawn aggregate lock; per-file unlinks make reruns converge."""
    planned = row.model_dump(exclude={"prompt"})

    def unlink() -> None:
        files = runner_stream_files(root, row.id)
        pruned.append(_pruned(root, row, files))
        for path in files:
            path.unlink(missing_ok=True)

    return mutate_published_spawn_artifact(
        root,
        SpawnId(row.id),
        unlink,
        can_mutate=lambda current: (
            current.model_dump(exclude={"prompt"}) == planned
            and all(path.is_file() for path in natives)
        ),
    )


def _pruned(root: Path, row: SpawnRecord, files: tuple[Path, ...]) -> PrunedSpawn:
    return PrunedSpawn(
        spawn_id=row.id,
        chat_id=row.chat_id,
        bytes=_size(files),
        files=tuple(path.relative_to(root).as_posix() for path in files),
    )


__all__ = ["PRUNE_AFTER_DAYS", "PrunedSpawn", "RunnerHistoryPrune", "prune_runner_history"]
