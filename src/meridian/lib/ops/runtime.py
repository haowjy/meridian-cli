"""Operation runtime helpers for state/store resolution."""

import asyncio
import functools
import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, ParamSpec, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.catalog.mars_roots import MarsRoots, resolve_mars_roots
from meridian.lib.config.loader import load_config
from meridian.lib.config.models import MeridianConfig
from meridian.lib.config.project_paths import ProjectConfigPaths, resolve_project_config_paths
from meridian.lib.config.project_root import (
    ProjectRootSource,
    resolve_project_root_resolution,
)
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.sink import NullSink, OutputSink
from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.project_identity import (
    ensure_project_identity,
    project_identity_was_created,
    resolve_project_identity,
    state_id,
)
from meridian.lib.state.user_paths import get_project_home, get_user_home

if TYPE_CHECKING:
    from meridian.lib.bootstrap.services import RuntimeWriteContext

P = ParamSpec("P")
T = TypeVar("T")
RuntimeRootSource = Literal[
    "env",
    "user-home-project",
    "unresolved",
]


def _runtime_override_env_root(project_root: Path) -> Path | None:
    override = os.getenv("_MERIDIAN_RUNTIME_DIR", "").strip()
    if not override:
        return None
    candidate = Path(override).expanduser()
    return candidate if candidate.is_absolute() else project_root / candidate


def _runtime_dir_env_override_applies(*, ignore_runtime_env: bool = False) -> bool:
    """Return whether ``_MERIDIAN_RUNTIME_DIR`` may override derived runtime roots.

    Power-user override applies only at the primary Meridian root.  Nested
    processes derive runtime from ``MERIDIAN_PROJECT_DIR``; ``-C`` unsets
    ``_MERIDIAN_RUNTIME_DIR`` at process scope so this check is moot when
    the flag is active.
    """

    if ignore_runtime_env:
        return False
    from meridian.lib.core.depth import is_nested_meridian_process

    return not is_nested_meridian_process()


def async_from_sync(sync_fn: Callable[P, T]) -> Callable[P, Coroutine[Any, Any, T]]:
    @functools.wraps(sync_fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        from meridian.lib.core.logging import warning_cache_scope

        with warning_cache_scope():
            return await asyncio.to_thread(sync_fn, *args, **kwargs)

    return wrapper


class OperationRuntime(BaseModel):
    """Resolved dependencies used by operation handlers."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    project_root: Path
    authority: "RuntimeAuthoritySnapshot"
    config: MeridianConfig
    harness_registry: Any  # HarnessRegistry — typed as Any to avoid circular import
    sink: OutputSink = Field(default_factory=NullSink)

    @classmethod
    def from_prepared(
        cls,
        prepared: "RuntimeWriteContext",
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
            authority=prepared.authority,
            config=prepared.config,
            harness_registry=harness_registry,
            sink=sink or NullSink(),
        )


class RuntimeAuthoritySnapshot(BaseModel):
    """Resolved authority for project/runtime/config path decisions."""

    model_config = ConfigDict(frozen=True)

    execution_cwd: Path
    project_root: Path
    project_root_source: ProjectRootSource
    project_config_paths: ProjectConfigPaths
    project_state_dir: Path
    user_home: Path
    identity_tier: Literal["committed", "adhoc"]
    project_id: str | None
    project_identity_source: Literal["meridian.toml", "path", "unminted"]
    identity_created: bool = False
    mars_roots: MarsRoots
    runtime_root: Path | None = None
    runtime_root_source: RuntimeRootSource | None = None


@dataclass(frozen=True)
class ResolvedRoots:
    project_root: Path
    project_state_dir: Path
    runtime_root: Path
    execution_cwd: Path


def runtime_context(ctx: RuntimeContext | None) -> RuntimeContext:
    if ctx is not None:
        return ctx
    return RuntimeContext.from_environment()


def resolve_project_authority(
    project_root: str | Path | None = None,
    *,
    execution_cwd: Path | None = None,
) -> RuntimeAuthoritySnapshot:
    """Resolve the authoritative project-root snapshot for one invocation."""

    explicit_root: Path | None
    if isinstance(project_root, Path):
        explicit_root = project_root.expanduser().resolve()
    elif isinstance(project_root, str) and project_root.strip():
        explicit_root = Path(project_root).expanduser().resolve()
    else:
        explicit_root = None

    root_resolution = resolve_project_root_resolution(
        explicit_root,
        execution_cwd=execution_cwd,
    )
    identity = resolve_project_identity(root_resolution.project_root)
    project_paths = resolve_project_paths(root_resolution.project_root)
    project_identity_source: Literal["meridian.toml", "path", "unminted"] = (
        "path"
        if identity.tier == "adhoc"
        else "unminted"
        if identity.id is None
        else "meridian.toml"
    )
    return RuntimeAuthoritySnapshot(
        execution_cwd=root_resolution.execution_cwd,
        project_root=root_resolution.project_root,
        project_root_source=root_resolution.source,
        project_config_paths=resolve_project_config_paths(
            project_root=root_resolution.project_root,
            execution_cwd=root_resolution.execution_cwd,
        ),
        project_state_dir=project_paths.root_dir,
        user_home=get_user_home(),
        identity_tier=identity.tier,
        project_id=identity.id,
        project_identity_source=project_identity_source,
        mars_roots=resolve_mars_roots(root_resolution.project_root),
    )


def inspect_runtime_authority_for_read(
    project_root: str | Path | None = None,
    *,
    execution_cwd: Path | None = None,
    ignore_runtime_env: bool | None = None,
) -> RuntimeAuthoritySnapshot:
    """Resolve read authority without running migrations or writing state."""

    authority = resolve_project_authority(project_root, execution_cwd=execution_cwd)
    override_root = (
        _runtime_override_env_root(authority.project_root)
        if _runtime_dir_env_override_applies(
            ignore_runtime_env=bool(ignore_runtime_env),
        )
        else None
    )
    identity = resolve_project_identity(authority.project_root)
    project_id = state_id(identity)
    if override_root is not None:
        runtime_root = override_root
    elif project_id is not None:
        runtime_root = get_project_home(project_id)
    else:
        runtime_root = None
    if runtime_root is None:
        runtime_source: RuntimeRootSource = "unresolved"
    elif override_root is not None and runtime_root == override_root:
        runtime_source = "env"
    else:
        runtime_source = "user-home-project"
    return authority.model_copy(
        update={
            "runtime_root": runtime_root,
            "runtime_root_source": runtime_source,
        }
    )


def resolve_runtime_authority_for_read(
    project_root: str | Path | None = None,
    *,
    execution_cwd: Path | None = None,
    ignore_runtime_env: bool | None = None,
) -> RuntimeAuthoritySnapshot:
    """Resolve read authority, including the once-only legacy native-key import."""

    authority = inspect_runtime_authority_for_read(
        project_root,
        execution_cwd=execution_cwd,
        ignore_runtime_env=ignore_runtime_env,
    )
    if authority.runtime_root is not None:
        from meridian.lib.ops.legacy_native_import import maybe_import_legacy_native_sessions

        maybe_import_legacy_native_sessions(authority.runtime_root)
    return authority


def plan_runtime_authority_for_write(
    project_root: str | Path | None = None,
    *,
    execution_cwd: Path | None = None,
    ignore_runtime_env: bool | None = None,
) -> RuntimeAuthoritySnapshot:
    """Resolve the runtime path a write would use without materializing identity or state."""

    authority = resolve_project_authority(project_root, execution_cwd=execution_cwd)
    override = (
        _runtime_override_env_root(authority.project_root)
        if _runtime_dir_env_override_applies(
            ignore_runtime_env=bool(ignore_runtime_env),
        )
        else None
    )
    if override is not None:
        runtime_root = override
        runtime_source: RuntimeRootSource = "env"
    elif authority.project_id is not None:
        runtime_root = get_project_home(authority.project_id)
        runtime_source = "user-home-project"
    else:
        runtime_root = None
        runtime_source = "unresolved"
    return authority.model_copy(
        update={
            "runtime_root": runtime_root,
            "runtime_root_source": runtime_source,
        }
    )


def resolve_runtime_authority_for_write(
    project_root: str | Path | None = None,
    *,
    execution_cwd: Path | None = None,
    ignore_runtime_env: bool | None = None,
) -> RuntimeAuthoritySnapshot:
    """Resolve project/runtime authority for mutating callers."""

    authority = plan_runtime_authority_for_write(
        project_root,
        execution_cwd=execution_cwd,
        ignore_runtime_env=ignore_runtime_env,
    )
    identity_created = False
    ensured_project_id = authority.project_id
    if authority.runtime_root_source == "env":
        runtime_root = authority.runtime_root
        if runtime_root is None:
            raise RuntimeError("Planned runtime environment override has no root.")
    else:
        prior = resolve_project_identity(authority.project_root)
        identity = ensure_project_identity(authority.project_root)
        identity_created = project_identity_was_created(prior, identity)
        if identity.id is None:
            raise ValueError("Runtime write identity did not resolve a project ID.")
        ensured_project_id = identity.id
        runtime_root = get_project_home(identity.id)
    from meridian.lib.ops.legacy_native_import import maybe_import_legacy_native_sessions

    maybe_import_legacy_native_sessions(runtime_root)
    return authority.model_copy(
        update={
            "runtime_root": runtime_root,
            "runtime_root_source": (
                "env" if authority.runtime_root_source == "env" else "user-home-project"
            ),
            "project_id": ensured_project_id,
            "identity_created": identity_created,
        }
    )


def resolve_runtime_root_and_config(
    project_root: str | None = None,
    *,
    sink: OutputSink | None = None,
) -> tuple[Path, MeridianConfig]:
    """Resolve repository root and load operational config."""

    _ = sink
    authority = resolve_runtime_authority_for_write(project_root)
    from meridian.lib.ops.config import ensure_runtime_state_bootstrap_sync

    ensure_runtime_state_bootstrap_sync(authority.project_root)
    return authority.project_root, load_config(authority.project_root, authority=authority)


def resolve_runtime_root_and_config_for_read(
    project_root: str | None = None,
    *,
    sink: OutputSink | None = None,
) -> tuple[Path, MeridianConfig]:
    """Resolve repository root and load config without bootstrapping runtime state."""

    _ = sink
    authority = resolve_project_authority(project_root)
    return authority.project_root, load_config(authority.project_root, authority=authority)


def build_runtime_from_root_and_config(
    project_root: Path,
    config: MeridianConfig,
    *,
    authority: RuntimeAuthoritySnapshot | None = None,
    sink: OutputSink | None = None,
) -> OperationRuntime:
    """Build a runtime bundle from one pre-resolved root and config."""

    from meridian.lib.harness.registry import get_default_harness_registry

    resolved_authority = authority or resolve_runtime_authority_for_write(project_root)
    if resolved_authority.runtime_root is None:
        raise ValueError("Runtime write authority did not resolve a runtime root.")
    return OperationRuntime(
        project_root=resolved_authority.project_root,
        authority=resolved_authority,
        config=config,
        harness_registry=get_default_harness_registry(),
        sink=sink or NullSink(),
    )


def build_runtime(
    project_root: str | None = None,
    *,
    sink: OutputSink | None = None,
) -> OperationRuntime:
    """Build a runtime bundle rooted at one repository path."""

    authority = resolve_runtime_authority_for_write(project_root)
    from meridian.lib.ops.config import ensure_runtime_state_bootstrap_sync

    ensure_runtime_state_bootstrap_sync(authority.project_root)
    config = load_config(authority.project_root, authority=authority)
    return build_runtime_from_root_and_config(
        authority.project_root,
        config,
        authority=authority,
        sink=sink,
    )


def resolve_runtime_root(project_root: Path) -> Path:
    """Resolve runtime state root for write paths."""

    authority = resolve_runtime_authority_for_write(project_root)
    if authority.runtime_root is None:
        raise ValueError("Runtime write authority did not resolve a runtime root.")
    return authority.runtime_root


def resolve_runtime_root_or_none(project_root: Path) -> Path | None:
    """Resolve runtime state root for read paths, returning None when uninitialized."""

    return resolve_runtime_authority_for_read(project_root).runtime_root


def resolve_runtime_root_for_read(project_root: Path) -> Path | None:
    """Resolve runtime state root for reads, or ``None`` without identity."""

    return resolve_runtime_authority_for_read(project_root).runtime_root


def resolve_roots(project_root: str | None) -> ResolvedRoots:
    authority = resolve_runtime_authority_for_write(project_root)
    if authority.runtime_root is None:
        raise ValueError("Runtime write authority did not resolve a runtime root.")
    return ResolvedRoots(
        project_root=authority.project_root,
        project_state_dir=authority.project_state_dir,
        runtime_root=authority.runtime_root,
        execution_cwd=authority.execution_cwd,
    )


def resolve_roots_for_read(project_root: str | None) -> ResolvedRoots | None:
    authority = resolve_runtime_authority_for_read(project_root)
    if authority.runtime_root is None:
        return None
    return ResolvedRoots(
        project_root=authority.project_root,
        project_state_dir=authority.project_state_dir,
        runtime_root=authority.runtime_root,
        execution_cwd=authority.execution_cwd,
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
