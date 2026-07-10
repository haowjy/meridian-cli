"""Persistent state for artifact serves."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.user_paths import get_user_home


@dataclass(frozen=True)
class Serve:
    """One detached artifact server and its Tailscale proxy."""

    slug: str
    local_port: int
    dir: str
    work_id: str | None
    created: str
    ttl_seconds: int | None
    funnel: bool
    pid: int
    process_created_at: float | None = None


_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def is_valid_slug(slug: str) -> bool:
    """Return whether *slug* is a canonical safe URL path component."""
    return 1 <= len(slug) <= 64 and _SLUG_PATTERN.fullmatch(slug) is not None


def _is_valid_created(created: str) -> bool:
    if not created.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(created.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def serves_path() -> Path:
    """Return the user-level artifact serve state file."""
    return get_user_home() / "serves.json"


def serves_lock_path() -> Path:
    """Return the lock protecting artifact state transactions."""
    path = serves_path()
    return path.with_name(f"{path.name}.lock")


def _serve_from_json(value: object) -> Serve | None:
    if not isinstance(value, dict):
        return None
    fields = cast("dict[str, object]", value)
    slug = fields.get("slug")
    local_port = fields.get("local_port")
    directory = fields.get("dir")
    work_id = fields.get("work_id")
    created = fields.get("created")
    ttl_seconds = fields.get("ttl_seconds")
    funnel = fields.get("funnel")
    pid = fields.get("pid")
    process_created_at = fields.get("process_created_at")
    if (
        not isinstance(slug, str)
        or not is_valid_slug(slug)
        or not isinstance(local_port, int)
        or isinstance(local_port, bool)
        or not 1 <= local_port <= 65535
        or not isinstance(directory, str)
        or (work_id is not None and not isinstance(work_id, str))
        or not isinstance(created, str)
        or not _is_valid_created(created)
        or (ttl_seconds is not None and not isinstance(ttl_seconds, int))
        or isinstance(ttl_seconds, bool)
        or (isinstance(ttl_seconds, int) and ttl_seconds <= 0)
        or not isinstance(funnel, bool)
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or (
            process_created_at is not None
            and not isinstance(process_created_at, (int, float))
        )
        or isinstance(process_created_at, bool)
        or (
            isinstance(process_created_at, (int, float))
            and process_created_at <= 0
        )
    ):
        return None
    created_at = float(process_created_at) if process_created_at is not None else None
    return Serve(
        slug, local_port, directory, work_id, created, ttl_seconds, funnel, pid, created_at
    )


def load_serves() -> list[Serve]:
    """Load valid serve entries, tolerating missing or damaged state."""
    try:
        value: object = json.loads(serves_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(value, list):
        return []
    items = cast("list[object]", value)
    serves: list[Serve] = []
    slugs: set[str] = set()
    ports: set[int] = set()
    for item in items:
        serve = _serve_from_json(item)
        if serve is None or serve.slug in slugs or serve.local_port in ports:
            continue
        serves.append(serve)
        slugs.add(serve.slug)
        ports.add(serve.local_port)
    return serves


def save_serves(serves: list[Serve]) -> None:
    """Atomically replace artifact serve state."""
    path = serves_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps([asdict(serve) for serve in serves], indent=2) + "\n")


__all__ = [
    "Serve",
    "is_valid_slug",
    "load_serves",
    "save_serves",
    "serves_lock_path",
    "serves_path",
]
