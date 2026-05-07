"""Persisted active-work pointer for non-session CLI flows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import RuntimePaths


class CurrentWorkState:
    """Serializable payload for persisted current-work state."""

    def __init__(self, *, work_id: str) -> None:
        self.work_id = work_id

    def as_json(self) -> str:
        return json.dumps({"work_id": self.work_id}, indent=2, sort_keys=True) + "\n"


def _normalize_work_id(work_id: str | None) -> str | None:
    normalized = (work_id or "").strip()
    return normalized or None


def _read_payload(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return cast("dict[str, Any]", loaded)


def _read_current_work_id_unlocked(path: Path) -> str | None:
    payload = _read_payload(path)
    if payload is None:
        return None
    work_id = payload.get("work_id")
    return _normalize_work_id(work_id if isinstance(work_id, str) else None)


def get_current_work_id(runtime_root: Path) -> str | None:
    """Return persisted current work ID for one runtime root, if present."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    with lock_file(paths.current_work_flock):
        return _read_current_work_id_unlocked(paths.current_work_json)


def set_current_work_id(runtime_root: Path, work_id: str | None) -> bool:
    """Persist or clear current work ID for one runtime root.

    Returns True when state changed.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    normalized = _normalize_work_id(work_id)

    with lock_file(paths.current_work_flock):
        existing = _read_current_work_id_unlocked(paths.current_work_json)
        if existing == normalized:
            return False

        if normalized is None:
            paths.current_work_json.unlink(missing_ok=True)
            return True

        atomic_write_text(
            paths.current_work_json,
            CurrentWorkState(work_id=normalized).as_json(),
        )
        return True


__all__ = ["get_current_work_id", "set_current_work_id"]
