"""Argv entry for the ``agy`` commands that run before the full CLI loads.

``harness-hook`` runs before every model call and at the end of every turn of a person's
TUI, and ``harness-watch`` is a background command of it, so they are dispatched from
``cli/entrypoint.py`` (through ``harness/plugin_hosted.py``) like ``--version``, importing
nothing beyond this package.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

_HOOK_EVENTS = ("SessionStart", "PreInvocation", "Stop")
_HOOK_USAGE = f"usage: meridian harness-hook agy <{'|'.join(_HOOK_EVENTS)}> --agy-pid PID\n"
_WATCH_USAGE = "usage: meridian harness-watch agy --agy-pid PID --conversation ID\n"


def run_command(argv: Sequence[str]) -> int:
    if argv[0] == "harness-mcp":
        from meridian.lib.harness.agy.mcp_server import run_server

        run_server()
        return 0
    if argv[0] == "harness-watch":
        return _run_watch(list(argv[2:]))
    return _run_hook(list(argv[2:]))


def _run_hook(args: list[str]) -> int:
    event = args[0] if args else ""
    agy_pid = _flag(args, "--agy-pid")
    if event not in _HOOK_EVENTS or agy_pid is None or not agy_pid.isdigit():
        sys.stderr.write(_HOOK_USAGE)
        return 2
    from meridian.lib.harness.agy.hook import run_hook

    reply = run_hook(event, int(agy_pid), sys.stdin.read())
    if reply is not None:
        sys.stdout.write(reply)
    return 0


def _run_watch(args: list[str]) -> int:
    agy_pid = _flag(args, "--agy-pid")
    conversation = _flag(args, "--conversation")
    if agy_pid is None or not agy_pid.isdigit() or not conversation:
        sys.stderr.write(_WATCH_USAGE)
        return 2
    from meridian.lib.harness.agy.watch import run

    return run(int(agy_pid), conversation)


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args[:-1] else None


__all__ = ["run_command"]
