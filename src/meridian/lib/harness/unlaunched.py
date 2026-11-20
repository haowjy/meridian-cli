"""The one entry for harnesses the person starts themselves (``agy``).

Their package holds what runs inside the person's session: ``commands.run_command(argv)``
for ``meridian harness-hook|harness-mcp <harness> ...``, and ``notices.send_owed_notices()``
for work a closed session left. Callers use this module and never name a harness. A new
one is a package with those two functions and a line in ``_PACKAGES``. Imports nothing
heavy: the hook runs before every model call and dispatches from ``cli/entrypoint.py``.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

_PACKAGES = {"agy": "meridian.lib.harness.agy"}


def run_command(argv: Sequence[str]) -> int | None:
    """Run an unlaunched harness's argv command, or return None when ``argv`` is not one."""

    if len(argv) < 2 or argv[0] not in ("harness-hook", "harness-mcp") or argv[1] not in _PACKAGES:
        return None
    commands = importlib.import_module(f"{_PACKAGES[argv[1]]}.commands")
    return commands.run_command(argv)


def send_owed_notices() -> None:
    """Tell the senders of messages that closed sessions never showed their model."""

    for package in _PACKAGES.values():
        importlib.import_module(f"{package}.notices").send_owed_notices()


__all__ = ["run_command", "send_owed_notices"]
