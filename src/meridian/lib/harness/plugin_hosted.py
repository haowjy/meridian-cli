"""The one entry for harnesses whose plugin hosts their sessions (``agy``).

The person starts the session and the harness's plugin (hook and MCP server) writes its
row, so Meridian runs no runner for it: nothing to cancel, repair or import. Whether
Meridian may exec the harness is the same fact: ``HarnessId.launched_by_meridian`` is false
exactly for these, and their launch is printed (``PrintedLaunchPorts``).

Their package holds what runs inside the person's session: ``commands.run_command(argv)``
for ``meridian harness-hook|harness-mcp|harness-watch <harness> ...``, and
``notices.send_owed_notices()`` for work a closed session left. It also says where the
sessions live: ``state.native_store()``; the package's ``adapter.py`` reads them. Callers use
this module and never name a harness. A new one is a package with those and its name in
``core/types.PLUGIN_HOSTED``.
``harness-watch`` is the background command that wakes an idle session; ``harness-hook`` also
runs at the end of every turn. Imports nothing heavy: both dispatch from ``cli/entrypoint.py``
and the hook runs before every model call.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

from meridian.lib.core.types import PLUGIN_HOSTED

_COMMANDS = ("harness-hook", "harness-mcp", "harness-watch")
_PACKAGES = {name: f"meridian.lib.harness.{name}" for name in PLUGIN_HOSTED}


def is_plugin_hosted(name: str | None) -> bool:
    """Whether a stored harness name is one whose plugin hosts its sessions.

    A name no build knows (a removed harness) is not: its rows keep their old rules.
    """

    return name in PLUGIN_HOSTED


def run_command(argv: Sequence[str]) -> int | None:
    """Run a plugin-hosted harness's argv command, or return None when ``argv`` is not one."""

    if len(argv) < 2 or argv[0] not in _COMMANDS or argv[1] not in _PACKAGES:
        return None
    commands = importlib.import_module(f"{_PACKAGES[argv[1]]}.commands")
    return commands.run_command(argv)


def native_store(name: str) -> str | None:
    """Where a plugin-hosted harness keeps its native sessions, or None for any other name.

    Fixed by the harness's own layout, so a chat's record never stores it and older builds
    read the record unchanged. Light: key completion calls it.
    """

    if name not in _PACKAGES:
        return None
    return str(importlib.import_module(f"{_PACKAGES[name]}.state").native_store())


def send_owed_notices() -> None:
    """Tell the senders of messages that closed sessions never showed their model."""

    for package in _PACKAGES.values():
        importlib.import_module(f"{package}.notices").send_owed_notices()


__all__ = [
    "is_plugin_hosted",
    "native_store",
    "run_command",
    "send_owed_notices",
]
