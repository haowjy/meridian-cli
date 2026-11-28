"""What a Meridian command asks of a harness without launching it.

``spawn wait``, ``spawn collect`` and ``idle mod-path`` run in short-lived processes that
load no adapter: building the registry imports every harness. So a harness that has an
answer declares it here as ``COMMAND_PORTS`` in a light module of its own, listed in
``_MODULES``; only the listed modules are imported, and a harness not listed answers with
the neutral value. Callers use this module and never name a harness module.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from meridian.lib.core.types import HarnessId
from meridian.lib.state.spawn_observations import observed_launches

OnRead = Callable[[Path, str | None], None]

_MODULES: dict[HarnessId, str] = {
    HarnessId.CLAUDE: "meridian.lib.harness.claude_runtime_resolver",
    HarnessId.PI: "meridian.lib.harness.pi_private_state",
}


@dataclass(frozen=True)
class CommandPorts:
    # Launch keys whose results the harness's own delivery recorded as reaching the
    # parent, beyond the shared record; a damaged source is skipped and reported.
    consumed_launches: Callable[[Path, str, OnRead | None], frozenset[str]] | None = None
    # Held while a Meridian command blocks its caller: a harness whose shell tasks can be
    # backgrounded or reaped marks the task busy for that long.
    blocking_command_hold: Callable[[], AbstractContextManager[None]] | None = None
    # Meridian's idle mod for the harness's own TUI, for an opt-in outside Meridian.
    idle_mod_dir: Callable[[], str | None] | None = None


_NONE = CommandPorts()


@cache
def get_command_ports(harness_id: HarnessId) -> CommandPorts:
    """The harness's declared ports, or empty ones."""
    module = _MODULES.get(harness_id)
    if module is None:
        return _NONE
    ports: CommandPorts = importlib.import_module(module).COMMAND_PORTS
    return ports


def _declared() -> tuple[CommandPorts, ...]:
    return tuple(get_command_ports(harness_id) for harness_id in _MODULES)


def consumed_launches(
    runtime_root: Path, parent_spawn_id: str, *, on_read: OnRead | None = None
) -> frozenset[str]:
    """Launch keys whose results reached one parent, whichever harness it runs: the shared
    record ``spawn collect`` and waits keep, and each harness's own delivery record."""
    consumed = set(observed_launches(runtime_root, parent_spawn_id, on_read=on_read))
    for ports in _declared():
        if ports.consumed_launches is not None:
            consumed |= ports.consumed_launches(runtime_root, parent_spawn_id, on_read)
    return frozenset(consumed)


@contextmanager
def hold_blocking_command() -> Generator[None]:
    """Every harness's ``blocking_command_hold``, for a command that blocks its caller."""
    with ExitStack() as stack:
        for ports in _declared():
            if ports.blocking_command_hold is not None:
                stack.enter_context(ports.blocking_command_hold())
        yield


def idle_mod_dir(harness_id: HarnessId) -> str | None:
    """Where the harness's idle mod is; None when it has none."""
    ports = get_command_ports(harness_id)
    return ports.idle_mod_dir() if ports.idle_mod_dir is not None else None
