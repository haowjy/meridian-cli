"""The Python side of the autosync seam.

Writing: this project's root record, which tells the `meridian-sync` daemon what to sync.
Reading: the daemon's lock, `service.off` and `state.json`, turned into what each surface
renders. Python reads no clock for any of the decisions here; it compares values the
daemon and this module wrote.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from meridian.lib.autosync.clones import clone_key, enclosing_clone
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.atomic import atomic_write_text
from meridian.lib.state.user_paths import get_user_home

RECORD_V = 1
STATE_V = 1

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

SyncState = Literal["off", "pending", "synced", "retrying", "stopped"]


def now_iso() -> str:
    """UTC with six fractional digits and ``Z``: two values compare as strings."""

    return datetime.now(UTC).strftime(TIME_FORMAT)


def autosync_dir() -> Path:
    return get_user_home() / "autosync"


@dataclass(frozen=True)
class GitContext:
    """One of the project's git contexts, as the daemon is told of it."""

    name: str
    remote: str
    clone_path: Path
    path: str
    autosync: bool

    @property
    def clone_key(self) -> str:
        return clone_key(self.clone_path)

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "remote": self.remote,
            "clone_path": self.clone_path.as_posix(),
            "clone_key": self.clone_key,
            "path": self.path,
            "autosync": self.autosync,
        }


def git_contexts(project_root: Path) -> list[GitContext]:
    """The project's contexts that live in a git clone.

    Raises ``ValueError`` when the context config doesn't load. A context whose path
    can't be resolved (a ``{project}`` placeholder with no project id) or isn't inside its
    clone is left out: there is nothing in a clone for the daemon to sync for it.
    """

    from meridian.lib.config.context_config import ContextSourceType
    from meridian.lib.context.resolver import resolve_context_paths
    from meridian.lib.state.paths import load_context_config
    from meridian.lib.state.project_identity import resolve_project_identity, state_id
    from meridian.plugin_api.git import resolve_clone_path

    config = load_context_config(project_root)
    if config is None:
        return []
    identity = resolve_project_identity(project_root)
    resolved = resolve_context_paths(project_root, config, state_id(identity), identity=identity)

    managed_root = Path(os.path.realpath(get_user_home() / "git"))
    contexts: list[GitContext] = []
    for ctx in resolved.contexts:
        remote = ctx.remote.strip() if ctx.remote else ""
        if ctx.source != ContextSourceType.GIT or not remote or ctx.path is None:
            continue
        clone_path = resolve_clone_path(remote)
        # Normalized first: a `..` in a committed `meridian.toml` must not leave the clone.
        try:
            relative = Path(os.path.normpath(ctx.path)).relative_to(clone_path)
        except ValueError:
            continue
        contexts.append(
            GitContext(
                name=ctx.name,
                remote=remote,
                clone_path=clone_path,
                path="" if relative == Path() else relative.as_posix(),
                # Unset: a managed clone syncs, a user's own checkout doesn't.
                autosync=(
                    ctx.autosync if ctx.autosync is not None else clone_path.parent == managed_root
                ),
            )
        )
    return contexts


def record_path(project_root: Path) -> Path:
    """``autosync/roots/<sha16(realpath root)>.json``."""

    digest = hashlib.sha256(os.path.realpath(project_root).encode("utf-8")).hexdigest()[:16]
    return autosync_dir() / "roots" / f"{digest}.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def read_record_written_at(project_root: Path) -> str | None:
    """The ``written_at`` of this project's record, or ``None`` when it has none."""

    record = _read_json(record_path(project_root))
    written_at = record.get("written_at") if record else None
    return written_at if isinstance(written_at, str) else None


@dataclass(frozen=True)
class Registration:
    """This project's root record as written or confirmed."""

    written_at: str
    contexts: Sequence[GitContext]


def register_project(project_root: Path, *, force: bool = False) -> Registration | None:
    """Write this project's root record; return it, or ``None`` if no record applies (no
    git contexts, or a root inside a managed clone). A project that no longer has git
    contexts removes its own record, so the daemon drops the clone.

    Rewrites only when the contexts changed, unless ``force``: a new ``written_at`` is what
    asks the daemon for a pass. Raises ``ValueError`` for a config that fails to load and
    ``OSError`` when the file can't be written; the caller decides who hears of that.
    """

    root = Path(os.path.realpath(project_root))
    if enclosing_clone(root) is not None:
        return None
    git = git_contexts(root)
    contexts = [c.to_json() for c in git]
    path = record_path(root)
    if not contexts:
        path.unlink(missing_ok=True)
        return None
    existing = _read_json(path)
    if (
        not force
        and existing is not None
        and existing.get("v") == RECORD_V
        and existing.get("root") == root.as_posix()
        and existing.get("contexts") == contexts
        and isinstance(existing.get("written_at"), str)
    ):
        return Registration(cast("str", existing["written_at"]), git)
    written_at = now_iso()
    record = {
        "v": RECORD_V,
        "root": root.as_posix(),
        "written_at": written_at,
        "contexts": contexts,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(record, indent=2) + "\n")
    return Registration(written_at, git)


def prune_leftovers() -> int:
    """`meridian doctor --prune`: root records whose project is gone, and what the removed
    `git-autosync` hook left. Returns how many paths it removed."""

    removed = 0
    for record in (autosync_dir() / "roots").glob("*.json"):
        data = _read_json(record)
        root = data.get("root") if data else None
        if isinstance(root, str) and not Path(root).exists():
            removed += _remove(record)
    return removed + _prune_old_hook_files()


def _remove(path: Path) -> int:
    """1 if `path` is gone afterwards by this call, else 0."""

    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError:
        return 0
    return 1


def _prune_old_hook_files() -> int:
    """Migration, removed with the `git-autosync` stub (see `.context/TODO`): the old hook's
    per-clone locks and per-project state."""

    home = get_user_home()
    removed = 0
    for lock in (home / "locks").glob("clone-*.lock"):
        removed += _remove(lock)
    for state_dir in (home / "projects").glob("*/autosync"):
        removed += _remove(state_dir)
    return removed


# ---- Reading what the daemon says --------------------------------------------------


def daemon_running() -> bool:
    """Whether a daemon holds ``daemon.lock``: open without creating, try a shared lock."""

    if IS_WINDOWS:
        return False
    import fcntl

    try:
        fd = os.open(autosync_dir() / "daemon.lock", os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def service_off_path() -> Path:
    return autosync_dir() / "service.off"


def service_off() -> bool:
    return service_off_path().exists()


@dataclass(frozen=True)
class DaemonFile:
    """``state.json`` as read: the parsed file, or why it can't be used."""

    data: Mapping[str, Any] | None
    problem: str | None = None

    @property
    def clones(self) -> Mapping[str, Mapping[str, Any]]:
        clones = self.data.get("clones") if self.data else None
        return cast("Mapping[str, Mapping[str, Any]]", clones) if isinstance(clones, dict) else {}

    @property
    def skipped_records(self) -> Sequence[Mapping[str, Any]]:
        skipped = self.data.get("skipped_records") if self.data else None
        return cast("Sequence[Mapping[str, Any]]", skipped) if isinstance(skipped, list) else []

    def text(self, field: str) -> str | None:
        value = self.data.get(field) if self.data else None
        return value if isinstance(value, str) else None


def read_daemon_file() -> DaemonFile:
    path = autosync_dir() / "state.json"
    data = _read_json(path)
    if data is None:
        return DaemonFile(None)
    if data.get("v") != STATE_V:
        version = data.get("version", "an unknown version")
        return DaemonFile(
            None, f"written by meridian-sync {version}, which this meridian can't read"
        )
    return DaemonFile(data)


def not_running_fix() -> str:
    from meridian.lib.autosync.service import has_manager

    if has_manager():
        return "Start the service with `meridian sync service install`."
    return "Start the daemon with `meridian sync run`."


STARTING_NOTE = "the daemon is starting"
NOT_RUNNING_NOTE = "daemon not running"


@dataclass(frozen=True)
class CloneSync:
    """What one clone of this project is doing, and the contexts that name it."""

    contexts: tuple[str, ...]
    clone_key: str
    clone_path: str
    state: SyncState
    # The daemon's entry for the clone, when it has one.
    entry: Mapping[str, Any] | None = None
    # Why, when the state isn't read from an entry: not running, unreadable, starting.
    note: str | None = None
    fix: str | None = None

    @property
    def names(self) -> str:
        return ", ".join(self.contexts)


def clone_syncs(
    contexts: Sequence[GitContext],
    *,
    running: bool,
    service_is_off: bool,
    file: DaemonFile,
    written_at: str | None,
    record_file: str | None,
) -> list[CloneSync]:
    """The one decision every surface renders: one row per clone, `autosync` on when any
    context naming the clone has it. A running daemon's entry, else off, else pending
    with a note saying why."""

    by_clone: dict[str, list[GitContext]] = {}
    for ctx in contexts:
        by_clone.setdefault(ctx.clone_key, []).append(ctx)
    out: list[CloneSync] = []
    for key, members in by_clone.items():
        names = tuple(c.name for c in members)
        path = members[0].clone_path.as_posix()
        if not running:
            if service_is_off or not any(c.autosync for c in members):
                out.append(CloneSync(names, key, path, "off"))
            else:
                out.append(
                    CloneSync(
                        names,
                        key,
                        path,
                        "pending",
                        note=NOT_RUNNING_NOTE,
                        fix=not_running_fix(),
                    )
                )
            continue
        if file.problem is not None:
            out.append(CloneSync(names, key, path, "pending", note=file.problem))
            continue
        entry = file.clones.get(key)
        if entry is not None:
            state = cast("SyncState", entry.get("state", "pending"))
            fix = entry.get("fix")
            out.append(
                CloneSync(
                    names,
                    key,
                    path,
                    state,
                    entry=entry,
                    fix=fix if isinstance(fix, str) else None,
                )
            )
            continue
        scanned_at = file.text("scanned_at")
        if file.data is None or (written_at and (scanned_at is None or scanned_at < written_at)):
            out.append(CloneSync(names, key, path, "pending", note=STARTING_NOTE))
            continue
        line = next((s for s in file.skipped_records if s.get("file") == record_file), None)
        detail = f" ({line.get('file')}: {line.get('error')})" if line else ""
        out.append(
            CloneSync(
                names,
                key,
                path,
                "stopped",
                note=f"the daemon did not pick up this context{detail}",
            )
        )
    return out
