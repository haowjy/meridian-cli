"""Interval parsing and last-success persistence for hook throttling."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeVar

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import RuntimePaths

_INTERVAL_PATTERN = re.compile(r"^(\d+)([smhd])$")
_INTERVAL_UNITS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}


class _HookRunResult(Protocol):
    success: bool
    skipped: bool


_ResultT = TypeVar("_ResultT", bound=_HookRunResult)


def parse_interval(interval: str) -> timedelta:
    """Parse an interval string like ``10m`` or ``1h``."""

    match = _INTERVAL_PATTERN.fullmatch(interval)
    if match is None:
        raise ValueError(f"Invalid interval format: {interval!r}. Expected '\\d+[smhd]'.")

    value = int(match.group(1))
    unit = match.group(2)
    return value * _INTERVAL_UNITS[unit]


def _hook_key(hook_name: str) -> str:
    """Map an arbitrary configured hook name to one fixed-size safe filename."""

    return hashlib.sha256(hook_name.encode("utf-8")).hexdigest()


class IntervalTracker:
    """Serialize interval decisions and successful executions per hook name."""

    def __init__(self, runtime_root: Path) -> None:
        paths = RuntimePaths.from_root_dir(runtime_root)
        self._last_run_dir = paths.hooks_last_run_dir
        self._locks_dir = paths.hook_locks_dir

    def run_if_due(
        self,
        hook_name: str,
        interval: str | None,
        fn: Callable[[], _ResultT],
    ) -> _ResultT | None:
        """Run ``fn`` once when due and persist its successful completion.

        The lock is deliberately non-reentrant: nesting the same hook execution would
        otherwise let an inner run invalidate the outer run's interval decision.
        An ``interval`` of ``None`` forces a serialized run for the explicit manual-run
        path. A ``None`` return means the hook was throttled and ``fn`` was not called.
        """

        key = _hook_key(hook_name)
        state_path = self._last_run_dir / key
        lock_path = self._locks_dir / f"{key}.lock"
        with lock_file(lock_path, reentrant=False):
            if not self._is_due(state_path, interval):
                return None

            result = fn()
            if result.success and not result.skipped:
                atomic_write_text(state_path, datetime.now(UTC).isoformat() + "\n")
            return result

    @staticmethod
    def _is_due(state_path: Path, interval: str | None) -> bool:
        if interval is None:
            return True
        try:
            last_success_raw = state_path.read_text(encoding="utf-8").strip()
            last_success = datetime.fromisoformat(last_success_raw)
            if last_success.tzinfo is None:
                last_success = last_success.replace(tzinfo=UTC)
            return datetime.now(UTC) - last_success >= parse_interval(interval)
        except (OSError, TypeError, ValueError):
            return True
