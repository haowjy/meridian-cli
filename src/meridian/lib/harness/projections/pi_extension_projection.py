"""Meridian-owned Pi extension projection helpers."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Final
from uuid import uuid4

from meridian.lib.state.user_paths import get_user_home

_EXTENSION_SOURCE_ROOT_OVERRIDE: Final[str] = "MERIDIAN_PI_EXTENSION_SOURCE_ROOT"
_EXTENSION_TARGET_ROOT_OVERRIDE: Final[str] = "MERIDIAN_PI_EXTENSION_TARGET_ROOT"

_REQUIRED_EXTENSION_RELATIVE_PATHS: Final[tuple[tuple[str, str], ...]] = (
    ("managed-bash", "managed-bash/index.js"),
    ("meridian-lifecycle", "meridian-lifecycle/index.js"),
)


class PiExtensionProjectionError(RuntimeError):
    """Raised when required Pi extension artifacts cannot be projected."""


def resolve_pi_extension_entrypoints() -> tuple[str, ...]:
    """Resolve and materialize Meridian-owned Pi extension entrypoints."""

    source_root = _resolve_extension_source_root()
    target_root = _resolve_extension_target_root()

    resolved_entrypoints: list[str] = []
    for extension_name, relative_path in _REQUIRED_EXTENSION_RELATIVE_PATHS:
        source_path = source_root / relative_path
        if not source_path.is_file():
            raise PiExtensionProjectionError(
                "Missing Pi extension artifact: "
                f"{source_path}. Build runtime extensions first "
                "(scripts/build-meridian-pi-runtime.sh or "
                "cd src/meridian/pi_runtime && bun run build:extensions)."
            )
        target_path = target_root / extension_name / "index.js"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy2(source_path, target_path)
        resolved_entrypoints.append(str(target_path))

    return tuple(resolved_entrypoints)


def _resolve_extension_source_root() -> Path:
    override = os.environ.get(_EXTENSION_SOURCE_ROOT_OVERRIDE)
    if override:
        return Path(override).expanduser().resolve()

    return (
        Path(__file__).resolve().parents[3]
        / "pi_runtime"
        / "dist"
        / "extensions"
    )


def _atomic_copy2(source_path: Path, target_path: Path) -> None:
    fd, temp_raw_path = tempfile.mkstemp(
        dir=target_path.parent,
        prefix=f".{target_path.name}.tmp-",
    )
    os.close(fd)
    temp_path = Path(temp_raw_path)
    try:
        shutil.copy2(source_path, temp_path)
        os.replace(temp_path, target_path)
    finally:
        with suppress(FileNotFoundError):
            temp_path.unlink()


def _resolve_extension_target_root() -> Path:
    override = os.environ.get(_EXTENSION_TARGET_ROOT_OVERRIDE)
    if override:
        return Path(override).expanduser().resolve()
    launch_id = uuid4().hex
    return get_user_home() / "meridian-pi" / "agent" / "extensions" / launch_id


__all__ = [
    "PiExtensionProjectionError",
    "resolve_pi_extension_entrypoints",
]
