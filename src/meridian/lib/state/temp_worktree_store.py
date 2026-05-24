"""Session-scoped temporary worktree metadata store."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.state.atomic import atomic_write_text

_SAFE_KEY = re.compile(r"[^a-zA-Z0-9._-]+")


class TemporaryWorktreeRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    repo_path: str
    worktree_name: str
    worktree_path: str
    branch: str
    status: Literal["pending", "ready"] = "ready"
    managed: bool = True
    updated_at: str


def _record_dir(runtime_root: Path) -> Path:
    return runtime_root / "worktree-temp"


def _safe_key(key: str) -> str:
    normalized = key.strip() or "default"
    return _SAFE_KEY.sub("-", normalized)


def _record_path(runtime_root: Path, key: str) -> Path:
    return _record_dir(runtime_root) / f"{_safe_key(key)}.json"


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def get_temporary_worktree(runtime_root: Path, key: str) -> TemporaryWorktreeRecord | None:
    path = _record_path(runtime_root, key)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return TemporaryWorktreeRecord.model_validate(cast("dict[str, object]", payload))
    except Exception:
        return None


def put_temporary_worktree(
    runtime_root: Path,
    *,
    key: str,
    repo_path: str,
    worktree_name: str,
    worktree_path: str,
    branch: str,
    status: Literal["pending", "ready"] = "ready",
    managed: bool = True,
) -> TemporaryWorktreeRecord:
    record = TemporaryWorktreeRecord(
        key=key,
        repo_path=repo_path,
        worktree_name=worktree_name,
        worktree_path=worktree_path,
        branch=branch,
        status=status,
        managed=managed,
        updated_at=_now_iso(),
    )
    target_dir = _record_dir(runtime_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        _record_path(runtime_root, key),
        json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
    )
    return record


def clear_temporary_worktree(runtime_root: Path, key: str) -> None:
    path = _record_path(runtime_root, key)
    try:
        path.unlink()
    except FileNotFoundError:
        return


__all__ = [
    "TemporaryWorktreeRecord",
    "clear_temporary_worktree",
    "get_temporary_worktree",
    "put_temporary_worktree",
]
