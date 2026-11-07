from __future__ import annotations

import sys


def module_argv(module: str, *args: str) -> list[str]:
    # Avoid loading a cwd package outside the harness sandbox; -P cannot protect
    # against empty or relative PYTHONPATH entries.
    return [sys.executable, "-P", "-m", module, *args]


__all__ = ["module_argv"]
