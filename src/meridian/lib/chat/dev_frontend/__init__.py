"""Development frontend support for ``meridian chat --dev``."""

from __future__ import annotations

import os
from pathlib import Path

from meridian.lib.chat.dev_frontend.policy import (
    DevFrontendConfigurationError,
    resolve_dev_frontend_launcher,
)
from meridian.lib.chat.dev_frontend.supervisor import DevSupervisor


def resolve_dev_frontend_root(
    *,
    explicit: str | None = None,
    project_root: Path | None = None,
) -> Path | None:
    """Resolve the meridian-web source checkout for dev mode."""

    if explicit and explicit.strip():
        return Path(explicit).expanduser().resolve()

    env_root = os.environ.get("MERIDIAN_DEV_FRONTEND_ROOT")
    if env_root and env_root.strip():
        return Path(env_root).expanduser().resolve()

    if project_root is None:
        return None
    try:
        sibling = project_root.parent / "meridian-web"
        if sibling.is_dir():
            return sibling.resolve()
    except Exception:
        return None
    return None


def validate_dev_prerequisites(frontend_root: Path) -> str | None:
    """Return an actionable error if the frontend checkout cannot run Vite."""

    if not frontend_root.is_dir():
        return f"Frontend root does not exist or is not a directory: {frontend_root}"
    if not (frontend_root / "package.json").is_file():
        return f"Frontend root is missing package.json: {frontend_root}"
    if not (frontend_root / "node_modules").is_dir():
        return (
            f"Frontend dependencies are missing at {frontend_root / 'node_modules'}. "
            f"Run: cd {frontend_root} && pnpm install"
        )
    return None


__all__ = [
    "DevFrontendConfigurationError",
    "DevSupervisor",
    "resolve_dev_frontend_launcher",
    "resolve_dev_frontend_root",
    "validate_dev_prerequisites",
]
