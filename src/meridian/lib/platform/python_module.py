"""Argv for re-launching this package's modules with the current interpreter."""

from __future__ import annotations

import sys


def module_argv(module: str, *args: str) -> list[str]:
    """Return `python -P -m <module> <args>` for `sys.executable`.

    `-P` drops the implicit working-directory entry from `sys.path`. Plain `-m` puts the
    cwd first, so a `meridian/` package in the project would run instead of this one, in a process
    Meridian started outside the harness sandbox. The package must therefore be importable
    from the interpreter's own environment (an install or `PYTHONPATH`); a bare
    `cd src && python -m meridian` launch is not reachable from a re-exec. `-P` does not
    cover `PYTHONPATH`: an empty or relative entry there still resolves to the cwd (#876).
    """

    return [sys.executable, "-P", "-m", module, *args]


__all__ = ["module_argv"]
