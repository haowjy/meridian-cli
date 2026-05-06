"""Operation runtime helpers for state/store resolution."""

import asyncio
import functools
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.bootstrap.services import RuntimeWriteContext
from meridian.lib.config.project_root import resolve_project_root
from meridian.lib.config.settings import MeridianConfig, load_config
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.sink import NullSink, OutputSink
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import (
    resolve_project_paths,
    resolve_project_runtime_root_for_write,
    resolve_project_runtime_root_or_none,
)
from meridian.lib.state.user_paths import (
    get_or_create_project_uuid,
)

P = ParamSpec("P")
T = TypeVar("T")


def async_from_sync(sync_fn: Callable[P, T]) -> Callable[P, Coroutine[Any, Any, T]]:
    @functools.wraps(sync_fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.to_thread(sync_fn, *args, **kwargs)

    return wrapper


class OperationRuntime(BaseModel):
    """Resolved dependencies used by operation handlers."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    project_root: Path
    config: MeridianConfig
    harness_registry: Any  # HarnessRegistry — typed as Any to avoid circular import
    artifacts: LocalStore
    sink: OutputSink = Field(default_factory=NullSink)

    @classmethod
    def from_prepared(
        cls,
        prepared: RuntimeWriteContext,
        *,
        harness_registry: Any,
        sink: OutputSink | None = None,
    ) -> "OperationRuntime":
        if prepared.config is None:
            raise ValueError("Prepared runtime write context is missing config.")
        if prepared.runtime_root is None:
            raise ValueError("Prepared runtime write context is missing runtime root.")
        return cls(
            project_root=prepared.project_root,
            config=prepared.config,
            harness_registry=harness_registry,
            artifacts=LocalStore(root_dir=prepared.runtime_root / "artifacts"),
            sink=sink or NullSink(),
        )


@dataclass(frozen=True)
class ResolvedRoots:
    project_root: Path
    project_state_dir: Path
    runtime_root: Path


def runtime_context(ctx: RuntimeContext | None) -> RuntimeContext:
    if ctx is not None:
        return ctx
    return RuntimeContext.from_environment()


def resolve_runtime_root_and_config(
    project_root: str | None = None,
    *,
    sink: OutputSink | None = None,
) -> tuple[Path, MeridianConfig]:
    """Resolve repository root and load operational config."""

    _ = sink
    explicit_root = Path(project_root).expanduser().resolve() if project_root else None
    resolved_root = resolve_project_root(explicit_root)
    from meridian.lib.ops.config import ensure_runtime_state_bootstrap_sync

    ensure_runtime_state_bootstrap_sync(resolved_root)
    return resolved_root, load_config(resolved_root)


def resolve_runtime_root_and_config_for_read(
    project_root: str | None = None,
    *,
    sink: OutputSink | None = None,
) -> tuple[Path, MeridianConfig]:
    """Resolve repository root and load config without bootstrapping runtime state."""

    _ = sink
    explicit_root = Path(project_root).expanduser().resolve() if project_root else None
    resolved_root = resolve_project_root(explicit_root)
    return resolved_root, load_config(resolved_root)


def build_runtime_from_root_and_config(
    project_root: Path,
    config: MeridianConfig,
    *,
    sink: OutputSink | None = None,
) -> OperationRuntime:
    """Build a runtime bundle from one pre-resolved root and config."""

    from meridian.lib.harness.registry import get_default_harness_registry

    return OperationRuntime(
        project_root=project_root,
        config=config,
        harness_registry=get_default_harness_registry(),
        artifacts=LocalStore(
            root_dir=resolve_project_runtime_root_for_write(project_root) / "artifacts"
        ),
        sink=sink or NullSink(),
    )


def build_runtime(
    project_root: str | None = None,
    *,
    sink: OutputSink | None = None,
) -> OperationRuntime:
    """Build a runtime bundle rooted at one repository path."""

    resolved_root, config = resolve_runtime_root_and_config(project_root, sink=sink)
    return build_runtime_from_root_and_config(resolved_root, config, sink=sink)


def resolve_runtime_root(project_root: Path) -> Path:
    """Resolve runtime state root for write paths."""

    return resolve_project_runtime_root_for_write(project_root)


def resolve_runtime_root_or_none(project_root: Path) -> Path | None:
    """Resolve runtime state root for read paths, returning None when uninitialized."""

    return resolve_project_runtime_root_or_none(project_root)


def resolve_runtime_root_for_read(project_root: Path) -> Path:
    """Resolve runtime state root for read paths without UUID creation.

    If a UUID exists but its user runtime root is still empty while repo-local
    state has data, prefer repo-local state as a compatibility fallback.
    """

    project_state_dir = resolve_project_paths(project_root).root_dir
    runtime_root = resolve_project_runtime_root_or_none(project_root)
    if runtime_root is None:
        return project_state_dir

    if runtime_root == project_state_dir:
        return runtime_root

    def has_spawn_state(root: Path) -> bool:
        spawns_dir = root / "spawns"
        return spawns_dir.is_dir() and any(spawns_dir.glob("*/state.json"))

    runtime_has_state = (
        has_spawn_state(runtime_root)
        or (runtime_root / "spawns.jsonl").is_file()
        or (runtime_root / "sessions.jsonl").is_file()
    )
    repo_has_state = (
        has_spawn_state(project_state_dir)
        or (project_state_dir / "spawns.jsonl").is_file()
        or (project_state_dir / "sessions.jsonl").is_file()
    )
    if not runtime_has_state and repo_has_state:
        return project_state_dir

    return runtime_root


def get_project_uuid(project_root: Path) -> str:
    """Get/create project UUID, returns UUID string."""

    return get_or_create_project_uuid(resolve_project_paths(project_root).root_dir)


def resolve_roots(project_root: str | None) -> ResolvedRoots:
    resolved_project_root, _ = resolve_runtime_root_and_config(project_root)
    project_state_dir = resolve_project_paths(resolved_project_root).root_dir
    return ResolvedRoots(
        project_root=resolved_project_root,
        project_state_dir=project_state_dir,
        runtime_root=resolve_runtime_root(resolved_project_root),
    )


def resolve_roots_for_read(project_root: str | None) -> ResolvedRoots:
    resolved_project_root, _ = resolve_runtime_root_and_config_for_read(project_root)
    project_state_dir = resolve_project_paths(resolved_project_root).root_dir
    return ResolvedRoots(
        project_root=resolved_project_root,
        project_state_dir=project_state_dir,
        runtime_root=resolve_runtime_root_for_read(resolved_project_root),
    )


def resolve_chat_id(
    *,
    payload_chat_id: str = "",
    ctx: RuntimeContext | None = None,
    fallback: str = "",
) -> str:
    normalized = payload_chat_id.strip()
    if normalized:
        return normalized
    resolved_chat_id = runtime_context(ctx).chat_id.strip()
    if resolved_chat_id:
        return resolved_chat_id
    return fallback.strip()
