"""Project root resolution helpers."""

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

USER_CONFIG_ENV_VAR = "MERIDIAN_CONFIG"
ProjectRootSource = Literal["explicit", "env", "cwd"]


class ProjectRootResolution(BaseModel):
    """Resolved project root plus discovery provenance."""

    model_config = ConfigDict(frozen=True)

    project_root: Path
    execution_cwd: Path
    source: ProjectRootSource


def cwd_has_project_id(cwd: Path) -> bool:
    """Return True when the literal cwd contains its own `.meridian/id` marker."""

    return (cwd / ".meridian" / "id").is_file()


def resolve_project_root_resolution(
    explicit: Path | None = None,
    *,
    execution_cwd: Path | None = None,
    ignore_env: bool = False,
) -> ProjectRootResolution:
    """Resolve project root and describe how it was discovered.

    ``ignore_env`` skips the ``MERIDIAN_PROJECT_DIR`` env var check, forcing
    CWD-based resolution. Use this when the caller must resolve relative to the
    actual CWD and not the inherited session project (e.g. hooks commands).
    """

    resolved_execution_cwd = (execution_cwd or Path.cwd()).expanduser().resolve()
    if explicit is not None:
        return ProjectRootResolution(
            project_root=explicit.expanduser().resolve(),
            execution_cwd=resolved_execution_cwd,
            source="explicit",
        )

    if not ignore_env:
        env_root = os.getenv("MERIDIAN_PROJECT_DIR")
        if env_root:
            return ProjectRootResolution(
                project_root=Path(env_root).expanduser().resolve(),
                execution_cwd=resolved_execution_cwd,
                source="env",
            )

    return ProjectRootResolution(
        project_root=resolved_execution_cwd,
        execution_cwd=resolved_execution_cwd,
        source="cwd",
    )


def resolve_user_config_path(user_config: Path | None) -> Path | None:
    from meridian.lib.state.user_paths import get_user_home

    resolved = user_config.expanduser() if user_config is not None else None
    if resolved is None:
        raw_env = os.getenv(USER_CONFIG_ENV_VAR, "").strip()
        if raw_env:
            resolved = Path(raw_env).expanduser()

    if resolved is None:
        default_user_config = get_user_home() / "config.toml"
        try:
            if default_user_config.is_file():
                return default_user_config
        except OSError:
            import logging

            from meridian.lib.core.depth import is_nested_meridian_process

            if is_nested_meridian_process():
                logging.getLogger(__name__).warning(
                    "Implicit user config path inaccessible in nested execution: %s",
                    default_user_config,
                )
            return None
        return None

    if not resolved.is_file():
        raise FileNotFoundError(f"User Meridian config file not found: '{resolved.as_posix()}'.")
    return resolved
