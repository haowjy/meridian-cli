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

_LIFECYCLE_EXTENSION_RELATIVE_PATH: Final[tuple[str, str]] = (
    "meridian-lifecycle",
    "meridian-lifecycle/index.js",
)

_ALL_REQUIRED_EXTENSION_RELATIVE_PATHS: Final[tuple[tuple[str, str], ...]] = (
    ("managed-bash", "managed-bash/index.js"),
    _LIFECYCLE_EXTENSION_RELATIVE_PATH,
)


class PiExtensionProjectionError(RuntimeError):
    """Raised when required Pi extension artifacts cannot be projected."""


def resolve_pi_lifecycle_extension_entrypoint() -> tuple[str, ...]:
    """Resolve and materialize the Meridian lifecycle Pi extension entrypoint."""

    source_root = _resolve_extension_source_root()
    target_root = _resolve_extension_target_root()
    return (_materialize_entrypoint(source_root, target_root, *_LIFECYCLE_EXTENSION_RELATIVE_PATH),)


def resolve_pi_all_extension_entrypoints() -> tuple[str, ...]:
    """Resolve and materialize all Meridian-owned Pi extension entrypoints."""

    source_root = _resolve_extension_source_root()
    target_root = _resolve_extension_target_root()
    return tuple(
        _materialize_entrypoint(source_root, target_root, extension_name, relative_path)
        for extension_name, relative_path in _ALL_REQUIRED_EXTENSION_RELATIVE_PATHS
    )



def _materialize_entrypoint(
    source_root: Path,
    target_root: Path,
    extension_name: str,
    relative_path: str,
) -> str:
    source_path = source_root / relative_path
    if not source_path.is_file():
        raise PiExtensionProjectionError(
            "Missing Pi extension artifact: "
            f"{source_path}. Build Pi extensions first "
            "(cd src/meridian/pi_runtime && npm run build:extensions)."
        )
    target_path = target_root / extension_name / "index.js"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_copy2(source_path, target_path)
    return str(target_path)


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
    "resolve_pi_all_extension_entrypoints",
    "resolve_pi_lifecycle_extension_entrypoint",
]
