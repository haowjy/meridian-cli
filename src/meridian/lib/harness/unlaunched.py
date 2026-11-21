"""The one entry for harnesses the person starts themselves (``agy``).

Their package holds what runs inside the person's session: ``commands.run_command(argv)``
for ``meridian harness-hook|harness-mcp <harness> ...``, and ``notices.send_owed_notices()``
for work a closed session left. It also says how Meridian reads the sessions:
``state.native_store()`` (where they live) and ``transcript.READER`` (a ``NativeStoreReader``).
Callers use this module and never name a harness. A new one is a package with those and a line
in ``_PACKAGES``. Imports nothing
heavy: the hook runs before every model call and dispatches from ``cli/entrypoint.py``.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meridian.lib.harness.adapter import NativeStoreReader

_PACKAGES = {"agy": "meridian.lib.harness.agy"}

# ``HarnessId.launched_by_meridian`` reads this, so the enum and the dispatch cannot drift.
NAMES = frozenset(_PACKAGES)


def run_command(argv: Sequence[str]) -> int | None:
    """Run an unlaunched harness's argv command, or return None when ``argv`` is not one."""

    if len(argv) < 2 or argv[0] not in ("harness-hook", "harness-mcp") or argv[1] not in _PACKAGES:
        return None
    commands = importlib.import_module(f"{_PACKAGES[argv[1]]}.commands")
    return commands.run_command(argv)


def native_store(name: str) -> str | None:
    """Where an unlaunched harness keeps its native sessions, or None for any other name.

    Fixed by the harness's own layout, so a chat's record never stores it and older builds
    read the record unchanged. Light: key completion calls it.
    """

    if name not in _PACKAGES:
        return None
    return str(importlib.import_module(f"{_PACKAGES[name]}.state").native_store())


def readers() -> dict[str, NativeStoreReader]:
    """Each unlaunched harness's reader of its native sessions, by harness name."""

    return {
        name: importlib.import_module(f"{package}.transcript").READER
        for name, package in _PACKAGES.items()
    }


def send_owed_notices() -> None:
    """Tell the senders of messages that closed sessions never showed their model."""

    for package in _PACKAGES.values():
        importlib.import_module(f"{package}.notices").send_owed_notices()


__all__ = ["NAMES", "native_store", "readers", "run_command", "send_owed_notices"]
