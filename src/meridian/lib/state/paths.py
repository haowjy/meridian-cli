"""Filesystem path helpers for file-authoritative Meridian state."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.config.context_config import ContextConfig
from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.config.project_root import resolve_user_config_path
from meridian.lib.core.types import SpawnId
from meridian.lib.state.project_identity import (
    ProjectIdentity,
    ensure_project_identity,
    resolve_project_identity,
    state_id,
)
from meridian.lib.state.user_paths import project_lifetime_lock_path

if TYPE_CHECKING:
    from meridian.lib.state.spawn.model import LaunchRef, SpawnRecord
    from meridian.lib.state.spawn.repository import StoredSpawnState


def normalize_path_for_write(path: str | None) -> str | None:
    """Return a stable absolute form for a path persisted in state."""

    if not path:
        return path
    return os.path.abspath(os.path.normpath(path))


class RuntimePaths(BaseModel):
    """Resolved runtime paths for one Meridian state root.

    This object models runtime state roots (spawn/session indexes and per-spawn
    artifacts).
    """

    model_config = ConfigDict(frozen=True)

    root_dir: Path
    spawns_jsonl: Path
    spawns_flock: Path
    sessions_jsonl: Path
    sessions_flock: Path
    hooks_last_run_dir: Path
    hook_locks_dir: Path
    session_id_counter: Path
    session_id_counter_flock: Path
    sessions_dir: Path
    kb_dir: Path
    work_dir: Path
    work_archive_dir: Path
    spawns_dir: Path

    @property
    def project_lifetime_flock(self) -> Path:
        """Return the stable gate coordinating use and deletion of this root."""

        return project_lifetime_lock_path(self.root_dir)

    @property
    def work_items_dir(self) -> Path:
        """Return the per-item link directory the work store publishes into."""

        return self.root_dir / "work-items"

    @classmethod
    def from_root_dir(cls, root_dir: Path) -> Self:
        """Build state-root-relative paths from an absolute state directory."""

        return cls(
            root_dir=root_dir,
            spawns_jsonl=root_dir / "spawns.jsonl",
            spawns_flock=root_dir / "spawns.jsonl.flock",
            sessions_jsonl=root_dir / "sessions.jsonl",
            sessions_flock=root_dir / "sessions.jsonl.flock",
            hooks_last_run_dir=root_dir / "hooks" / "last-run",
            hook_locks_dir=root_dir / "locks" / "hooks",
            session_id_counter=root_dir / "session-id-counter",
            session_id_counter_flock=root_dir / "session-id-counter.flock",
            sessions_dir=root_dir / "sessions",
            kb_dir=root_dir / "kb",
            work_dir=root_dir / "work",
            work_archive_dir=root_dir / "archive" / "work",
            spawns_dir=root_dir / "spawns",
        )


class ProjectPaths(BaseModel):
    """Resolved on-disk Meridian project state paths."""

    model_config = ConfigDict(frozen=True)

    root_dir: Path
    kb_dir: Path | None
    work_dir: Path | None
    work_archive_dir: Path | None

    @classmethod
    def from_root_dir(cls, root_dir: Path) -> Self:
        """Build project-state-relative paths from one state directory."""

        return cls(
            root_dir=root_dir,
            kb_dir=root_dir / "kb",
            work_dir=root_dir / "work",
            work_archive_dir=root_dir / "archive" / "work",
        )


def _context_config_paths(
    project_root: Path,
    *,
    project_config_paths: ProjectConfigPaths | None = None,
    user_config: Path | None = None,
    project_config: Path | None = None,
    local_config: Path | None = None,
) -> tuple[Path | None, Path, Path]:
    resolved_paths = project_config_paths
    return (
        resolve_user_config_path(user_config),
        project_config
        or (
            resolved_paths.meridian_toml
            if resolved_paths is not None
            else project_root / "meridian.toml"
        ),
        local_config
        or (
            resolved_paths.meridian_local_toml
            if resolved_paths is not None
            else project_root / "meridian.local.toml"
        ),
    )


def _load_context_table(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        payload_obj = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in Meridian config '{path.as_posix()}': {exc}") from exc

    payload = cast("dict[str, object]", payload_obj)
    context = payload.get("context")
    if context is None:
        return None
    if not isinstance(context, dict):
        raise ValueError(f"Invalid value for 'context' in '{path.as_posix()}': expected table.")
    return cast("dict[str, object]", context)


def _merge_nested_dicts(base: dict[str, object], overrides: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in overrides.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_nested_dicts(
                cast("dict[str, object]", current),
                cast("dict[str, object]", value),
            )
            continue
        merged[key] = value
    return merged


def resolve_project_paths(project_root: Path) -> ProjectPaths:
    """Resolve project-owned `.meridian/` paths only (ignores runtime overrides)."""

    return resolve_project_paths_from_context(project_root)


def resolve_project_paths_for_write(project_root: Path) -> ProjectPaths:
    """Resolve project-owned state paths for write flows.

    If context paths contain ``{project}``, this ensures project identity before
    path substitution so write callers never materialize literal placeholders.
    """

    return resolve_project_paths_from_context(project_root, create_project_uuid=True)


def _try_load_context_config(
    project_root: Path,
    *,
    project_config_paths: ProjectConfigPaths | None = None,
    user_config: Path | None = None,
    project_config: Path | None = None,
    local_config: Path | None = None,
) -> ContextConfig | None:
    """Try loading merged context config from user/project/local Meridian config files."""

    merged_context: dict[str, object] = {}
    found_context = False
    for config_path in _context_config_paths(
        project_root,
        project_config_paths=project_config_paths,
        user_config=user_config,
        project_config=project_config,
        local_config=local_config,
    ):
        if config_path is None:
            continue
        context_table = _load_context_table(config_path)
        if context_table is None:
            continue
        found_context = True
        merged_context = _merge_nested_dicts(merged_context, context_table)

    if not found_context:
        return None

    try:
        return ContextConfig.model_validate(merged_context)
    except ValidationError as exc:
        raise ValueError(f"Invalid Meridian [context] configuration: {exc}") from exc


def load_context_config(
    project_root: Path,
    *,
    project_config_paths: ProjectConfigPaths | None = None,
    user_config: Path | None = None,
    project_config: Path | None = None,
    local_config: Path | None = None,
) -> ContextConfig | None:
    """Load merged context config for one repo, or ``None`` when no [context] exists."""

    return _try_load_context_config(
        project_root,
        project_config_paths=project_config_paths,
        user_config=user_config,
        project_config=project_config,
        local_config=local_config,
    )


def resolve_project_paths_from_context(
    project_root: Path,
    context_config: ContextConfig | None = None,
    *,
    project_config_paths: ProjectConfigPaths | None = None,
    create_project_uuid: bool = False,
    identity: ProjectIdentity | None = None,
) -> ProjectPaths:
    """Resolve project paths with optional context config, falling back to defaults.

    When no explicit config is provided and no file-level config exists, the
    ``ContextConfig()`` defaults (user-level placeholder paths) are used.
    """

    if context_config is None:
        context_config = _try_load_context_config(
            project_root,
            project_config_paths=project_config_paths,
        )

    # Default to ContextConfig() which has user-level placeholder defaults
    effective_config = context_config or ContextConfig()

    from meridian.lib.context.resolver import (
        context_uses_project_placeholder,
        resolve_context_paths,
    )

    project_state_dir = project_root / ".meridian"  # legacy carrier; never created
    resolved_identity = identity or (
        ensure_project_identity(project_root)
        if create_project_uuid
        else resolve_project_identity(project_root)
    )
    project_id = state_id(resolved_identity)
    if context_uses_project_placeholder(effective_config) and project_id is None:
        return ProjectPaths(
            root_dir=project_state_dir,
            kb_dir=None,
            work_dir=None,
            work_archive_dir=None,
        )

    resolved = resolve_context_paths(
        project_root,
        effective_config,
        project_id=project_id,
        identity=resolved_identity,
    )
    return ProjectPaths(
        root_dir=project_state_dir,
        kb_dir=resolved.kb_root,
        work_dir=resolved.work_root,
        work_archive_dir=resolved.work_archive,
    )


def resolve_runtime_paths(project_root: Path) -> ProjectPaths:
    """Resolve state paths for a mutating caller, creating identity if needed."""

    root_dir = resolve_project_runtime_root_for_write(project_root)
    return ProjectPaths.from_root_dir(root_dir)


def resolve_project_runtime_root(project_root: Path) -> Path:
    """Resolve runtime state root for read paths.

    This helper is read-only: it never creates project identity.
    Raises when no identity exists; zero-state command paths use the optional resolver.
    """

    runtime_root = resolve_project_runtime_root_or_none(project_root)
    if runtime_root is not None:
        return runtime_root
    raise ValueError("Project has no runtime state.")


def resolve_project_runtime_root_or_none(project_root: Path) -> Path | None:
    """Resolve runtime state root without mutation.

    Returns None when no project UUID has been initialized yet.
    """
    from meridian.lib.ops.runtime import resolve_runtime_authority_for_read

    return resolve_runtime_authority_for_read(project_root).runtime_root


def resolve_project_runtime_root_for_write(project_root: Path) -> Path:
    """Resolve runtime state root for write paths, creating project UUID if needed."""
    from meridian.lib.ops.runtime import resolve_runtime_authority_for_write

    authority = resolve_runtime_authority_for_write(project_root)
    if authority.runtime_root is None:
        raise ValueError("Runtime write authority did not resolve a runtime root.")
    return authority.runtime_root


def resolve_kb_dir(project_root: Path) -> Path | None:
    """Return the configured KB directory, or ``None`` without identity."""

    return resolve_project_paths(project_root).kb_dir


def resolve_work_scratch_dir_for_project(
    project_root: Path,
    work_id: str,
    *,
    project_paths: ProjectPaths | None = None,
) -> Path:
    """Return the authority-resolved work directory for a work item."""

    resolved_project_paths = project_paths or resolve_project_paths(project_root)
    if resolved_project_paths.work_dir is None:
        raise ValueError("Project has no identity; work path is unresolved.")
    return resolved_project_paths.work_dir / work_id


def resolve_spawn_dir(runtime_root: Path, spawn_id: SpawnId | str) -> Path:
    """Return the spawn directory. Spawn-level files (``state.json``, ``launch.lock``,
    ``scope.json``, ``observed-launches.json``, the reaper claim, the ambient ``work/``
    directory) live here for every launch; per-launch files go through ``LaunchPaths``."""

    return runtime_root / "spawns" / str(spawn_id)


# Per-launch file names. This module is the only place they appear as literals
# (``meridian.dev.check_launch_paths``, part of the fast gate, enforces it, and flags a
# private name imported elsewhere). Code that must recognize a name rather than reach a
# file (retention, transcript discovery, legacy import) uses ``LAUNCH_FILE_NAMES``.
_HISTORY_FILENAME = "history.jsonl"  # retired runner stream; never written
_NATIVE_SNAPSHOT_FILENAME = "native-transcript.jsonl"
_PROCESS_SCOPES_FILENAME = "process_scopes.json"
_STARTING_PROMPT_FILENAME = "starting-prompt.md"
_REPORT_FILENAME = "report.md"
_PI_RUNTIME_META_FILENAME = "pi_runtime_meta.json"
_BACKGROUND_STDOUT_FILENAME = "background-launcher.stdout.log"
_BACKGROUND_STDERR_FILENAME = "background-launcher.stderr.log"
_BG_WORKER_REQUEST_FILENAME = "bg-worker-request.json"
REAPER_CLEANUP_CLAIM_FILENAME = "reaper_cleanup_claim.json"  # spawn-level


@dataclass(frozen=True)
class LaunchFileNames:
    """Per-launch names for code that recognizes a name instead of reaching a file."""

    history: str = _HISTORY_FILENAME
    native_snapshot: str = _NATIVE_SNAPSHOT_FILENAME
    starting_prompt: str = _STARTING_PROMPT_FILENAME
    report: str = _REPORT_FILENAME
    pi_runtime_meta: str = _PI_RUNTIME_META_FILENAME
    retired_runner_stream: tuple[str, ...] = (_HISTORY_FILENAME, "last-observed-event.json")
    # Files the archive skips. Older builds still write the legacy ones; remove with
    # legacy_runner_alive once no lockless rows remain.
    retention_excluded: frozenset[str] = frozenset(
        {
            "heartbeat",
            "finalize-evidence.json",
            "launch-boundary.jsonl",
            _PROCESS_SCOPES_FILENAME,
            REAPER_CLEANUP_CLAIM_FILENAME,
            "restored-from.json",
        }
    )


LAUNCH_FILE_NAMES = LaunchFileNames()


def launch_subdir(n: int | None) -> Path:
    """A launch directory relative to its spawn directory (empty for a v3 row's launch 1)."""

    return Path() if n is None else Path("launches") / str(n)


def spawn_report_files(runtime_root: Path, spawn_id: SpawnId | str) -> tuple[Path, ...]:
    """Every launch's report that exists, newest first.

    ``launches/<n>/report.md`` by descending ``n``, then the spawn directory's own report:
    a v3 row's launch 1, which a continue leaves in place.
    """

    spawn_dir = resolve_spawn_dir(runtime_root, spawn_id)
    numbers = sorted(
        (int(entry.name) for entry in (spawn_dir / "launches").glob("*") if entry.name.isdigit()),
        reverse=True,
    )
    candidates = [spawn_dir / launch_subdir(n) / _REPORT_FILENAME for n in numbers]
    candidates.append(spawn_dir / _REPORT_FILENAME)
    return tuple(path for path in candidates if path.is_file())


def row_launch_subdir(launch: "LaunchRef | None") -> Path:
    """``launch_subdir`` for a row's ``launch`` field (None on a v3 row)."""

    return launch_subdir(launch.n if launch else None)


def launch_archive(runtime_root: Path, spawn_id: SpawnId | str, n: int) -> Path:
    """Launch ``n``'s finished row, copied by the holder that starts launch ``n+1``.

    Spawn-level because it sits under ``launches/<n>/`` whatever version the row was
    when launch ``n`` ran (a v3 row's launch 1 keeps its own files at the top level).
    """

    return resolve_spawn_dir(runtime_root, spawn_id) / launch_subdir(n) / "launch.json"


@dataclass(frozen=True)
class LaunchPaths:
    """One launch's identity (``runtime_root``, ``spawn_id``, ``n``) and every file it owns.

    This module is the only place that constructs it (``for_row``); the code that
    publishes a row returns the launch it wrote, and everyone else is handed that one.
    A launch's directory is ``spawns/<id>/launches/<n>/``; ``n`` is ``None`` only for a
    v3 row (``launch`` absent), whose launch 1 stays in the spawn directory, and for the
    dry-run preview. Spawn-level files stay on ``resolve_spawn_dir``.
    """

    runtime_root: Path
    spawn_id: SpawnId
    n: int | None

    @classmethod
    def for_row(cls, runtime_root: Path, row: "SpawnRecord | StoredSpawnState") -> Self:
        """The launch a published row names. The only way to reach an existing launch."""

        return cls(runtime_root, SpawnId(row.id), row.launch.n if row.launch else None)

    @classmethod
    def reserved(cls, runtime_root: Path, spawn_id: SpawnId, n: int) -> Self:
        """The launch ``spawn_store`` is about to publish as launch ``n`` of ``spawn_id``.

        Only ``spawn_store`` calls this (the gate enforces it): the code that assigns
        ``n`` names the launch before its row exists, so the bind phase can reference
        its files. Everyone else is handed the launch.
        """

        return cls(runtime_root, spawn_id, n)

    @classmethod
    def preview(cls, runtime_root: Path) -> Self:
        """A placeholder launch for a dry-run bind. It is never published or written.

        The fixed ID keeps it from naming a real spawn; the gate limits callers to the two
        dry-run entry points.
        """

        return cls(runtime_root, SpawnId("dry-run"), None)

    @property
    def dir(self) -> Path:
        return resolve_spawn_dir(self.runtime_root, self.spawn_id) / launch_subdir(self.n)

    @property
    def starting_prompt(self) -> Path:
        return self.dir / _STARTING_PROMPT_FILENAME

    @property
    def system_prompt(self) -> Path:
        return self.dir / "system-prompt.md"

    @property
    def legacy_prompt(self) -> Path:
        return self.dir / "prompt.md"

    @property
    def references(self) -> Path:
        return self.dir / "references.json"

    @property
    def projection_manifest(self) -> Path:
        return self.dir / "projection-manifest.json"

    @property
    def inline_reference_bytes(self) -> Path:
        return self.dir / "inline-file-reference-bytes.json"

    @property
    def report(self) -> Path:
        return self.dir / _REPORT_FILENAME

    @property
    def stderr_log(self) -> Path:
        return self.dir / "stderr.log"

    @property
    def output(self) -> Path:
        return self.dir / "output.jsonl"

    @property
    def launcher_log(self) -> Path:
        return self.dir / "launcher.log"

    @property
    def runner_lifecycle(self) -> Path:
        return self.dir / "runner-lifecycle.jsonl"

    @property
    def claude_boundary(self) -> Path:
        return self.dir / "claude-session-boundary.jsonl"

    @property
    def params(self) -> Path:
        return self.dir / "params.json"

    @property
    def primary_meta(self) -> Path:
        return self.dir / "primary_meta.json"

    @property
    def pi_runtime_meta(self) -> Path:
        return self.dir / _PI_RUNTIME_META_FILENAME

    @property
    def pi_lifecycle(self) -> Path:
        return self.dir / "pi-lifecycle.json"

    @property
    def pi_session_boundary(self) -> Path:
        return self.dir / "pi-session-boundary.json"

    @property
    def process_scopes(self) -> Path:
        return self.dir / _PROCESS_SCOPES_FILENAME

    @property
    def cancel(self) -> Path:
        return self.dir / "cancel.json"

    @property
    def runner_activity(self) -> tuple[Path, ...]:
        """Files only the runner appends to; their newest mtime is its last sign of life."""
        return (self.output, self.stderr_log, self.debug, self.runner_lifecycle, self.report)

    @property
    def accepted_signals(self) -> Path:
        return self.dir / "accepted-signals.json"

    @property
    def done_signal(self) -> Path:
        return self.dir / "done.signal"

    @property
    def rearm_signal(self) -> Path:
        return self.dir / "rearm.signal"

    @property
    def control_actions(self) -> Path:
        return self.dir / "control_actions.jsonl"

    @property
    def permission_cursors(self) -> Path:
        return self.dir / "permission_request_cursors.json"

    @property
    def debug(self) -> Path:
        return self.dir / "debug.jsonl"

    @property
    def native_transcript(self) -> Path:
        return self.dir / _NATIVE_SNAPSHOT_FILENAME

    @property
    def background_stdout(self) -> Path:
        return self.dir / _BACKGROUND_STDOUT_FILENAME

    @property
    def background_stderr(self) -> Path:
        return self.dir / _BACKGROUND_STDERR_FILENAME

    @property
    def bg_worker_request(self) -> Path:
        return self.dir / _BG_WORKER_REQUEST_FILENAME

    @property
    def permission_requests(self) -> Path:
        return self.dir / "permission_requests.jsonl"


def staged_starting_prompt(stage_dir: Path, n: int) -> Path:
    """Where ``start_spawn`` writes launch ``n``'s prompt inside its staging directory,
    before the directory is renamed into place as the spawn directory."""

    return stage_dir / launch_subdir(n) / _STARTING_PROMPT_FILENAME


def resolve_ambient_work_dir(
    project_root: Path,
    spawn_id: SpawnId | str,
    *,
    runtime_root: Path | None = None,
) -> Path:
    """Resolve the ambient artifact directory for a spawn (pure path math, no mkdir)."""

    resolved_root = runtime_root or resolve_project_runtime_root(project_root)
    return resolve_spawn_dir(resolved_root, spawn_id) / "work"


def observed_launches_path(runtime_root: Path, spawn_id: SpawnId | str) -> Path:
    """Consumed-launch, wait-lease and wait-pin record owned by one parent spawn."""

    return resolve_spawn_dir(runtime_root, spawn_id) / "observed-launches.json"


def observed_spawns_path(runtime_root: Path, spawn_id: SpawnId | str) -> Path:
    """The file older builds kept at ``observed_launches_path``'s place. Read-only."""

    return resolve_spawn_dir(runtime_root, spawn_id) / "observed-spawns.json"
