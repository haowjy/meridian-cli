from __future__ import annotations

import sys
from pathlib import Path


def module_argv(module: str, *args: str) -> list[str]:
    # Avoid loading a cwd package outside the harness sandbox; -P cannot protect
    # against empty or relative PYTHONPATH entries.
    return [sys.executable, "-P", "-m", module, *args]


def scripts_dir() -> Path:
    """Where this build's scripts (``meridian`` among them) are installed."""
    # Keep the wrapper path intact: uv tool scripts point at a symlinked Python
    # binary, and resolving it jumps out of the tool environment where sibling
    # scripts live.
    return Path(sys.executable).parent


def installed_executable(name: str) -> str | None:
    """An executable installed beside this interpreter, or None."""
    for candidate in (scripts_dir() / name, scripts_dir() / f"{name}.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


__all__ = ["installed_executable", "module_argv", "scripts_dir"]
