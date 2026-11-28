"""Project and runtime roots for a spawn operation, from a prepared context or the payload."""

from pathlib import Path

from meridian.lib.bootstrap.services import (
    RuntimeReadContext,
    RuntimeWriteContext,
)
from meridian.lib.config.models import MeridianConfig
from meridian.lib.context.runtime import RuntimeContext
from meridian.lib.ops.runtime import (
    resolve_project_authority,
    resolve_runtime_root_for_read,
    runtime_context,
)


def _resolve_project_root_input(project_root: str | None) -> Path:
    return resolve_project_authority(project_root).project_root


def project_root_from_prepared(prepared: RuntimeReadContext | RuntimeWriteContext) -> Path:
    return prepared.project_root


def config_from_prepared(prepared: RuntimeReadContext | RuntimeWriteContext) -> MeridianConfig:
    config = prepared.config
    if config is None:
        raise ValueError("Prepared runtime context is missing config.")
    return config


def runtime_root_from_prepared_for_read(
    prepared: RuntimeReadContext | RuntimeWriteContext,
    *,
    project_root: Path,
) -> Path:
    runtime_root = prepared.runtime_root
    resolved = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved is None:
        raise ValueError("Project has no runtime state.")
    return resolved


def resolve_spawn_read_authority(
    *,
    project_root: str | None,
    prepared: RuntimeReadContext | RuntimeWriteContext | None = None,
) -> tuple[Path, Path | None]:
    if prepared is not None:
        resolved_project_root = project_root_from_prepared(prepared)
        resolved_runtime_root = prepared.runtime_root
        return resolved_project_root, resolved_runtime_root

    resolved_project_root = _resolve_project_root_input(project_root)
    resolved_runtime_root = resolve_runtime_root_for_read(resolved_project_root)
    return resolved_project_root, resolved_runtime_root


def resolve_caller_spawn_id(spawn_id: str | None, ctx: RuntimeContext | None) -> str:

    candidate = (spawn_id or "").strip()
    if not candidate:
        candidate = str(runtime_context(ctx).spawn_id or "").strip()
    if not candidate:
        raise ValueError("Spawn ID is required. Pass spawn_id or set MERIDIAN_SPAWN_ID.")
    return candidate
