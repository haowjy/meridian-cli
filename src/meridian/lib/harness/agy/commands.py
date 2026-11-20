"""Argv entry for the ``agy`` commands that run before the full CLI loads.

``harness-hook`` runs before every model call of a person's TUI, so it is dispatched from
``cli/entrypoint.py`` like ``--version``, importing nothing beyond this package.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

_HOOK_USAGE = "usage: meridian harness-hook agy <SessionStart|PreInvocation> --agy-pid PID\n"


def is_agy_command(argv: Sequence[str]) -> bool:
    return len(argv) >= 2 and argv[0] in ("harness-hook", "harness-mcp") and argv[1] == "agy"


def run_agy_command(argv: Sequence[str]) -> int:
    if argv[0] == "harness-mcp":
        from meridian.lib.harness.agy.mcp_server import run_server

        run_server()
        return 0
    return _run_hook(list(argv[2:]))


def _run_hook(args: list[str]) -> int:
    event = args[0] if args else ""
    agy_pid = _flag(args, "--agy-pid")
    if event not in ("SessionStart", "PreInvocation") or agy_pid is None or not agy_pid.isdigit():
        sys.stderr.write(_HOOK_USAGE)
        return 2
    from meridian.lib.harness.agy.hook import run_hook

    reply = run_hook(event, int(agy_pid), sys.stdin.read())
    if reply is not None:
        sys.stdout.write(reply)
    return 0


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args[:-1] else None


__all__ = ["is_agy_command", "run_agy_command"]
