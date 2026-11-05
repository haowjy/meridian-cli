"""The terminal prompt for a continue that stopped because the chat's directory moved.

``primary_launch`` raises ``RelocateStop`` where a continue would fail on a moved
directory; ``launch_or_offer_relocate`` catches it outside the launch slot (the continue
holds its spawn's lock there, and relocate must not find its own chat busy) and, on a
terminal, offers to relocate. Without one, it re-raises the refusal, whose text carries
the hint.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from meridian.lib.ops.relocate_gate import adapter_for, moved_root
from meridian.lib.ops.session_relocate import SessionRelocateInput, session_relocate_sync
from meridian.lib.state import session_store
from meridian.lib.state.paths import resolve_project_runtime_root

_T = TypeVar("_T")


class RelocateStop(ValueError):
    """A continue stopped before launch on a moved directory.

    ``error`` is the refusal to raise when nothing can be offered. ``refused`` says the
    chat was already relocated and its harness refused its own change.
    """

    def __init__(
        self, error: ValueError, *, chat_id: str, project_root: Path, refused: bool
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.chat_id = chat_id
        self.project_root = project_root
        self.refused = refused


def _ask(options: dict[str, str]) -> str | None:
    """The chosen key, or None on end of input. Re-asks on anything else."""
    keys = "/".join(options)
    while True:
        for key, label in options.items():
            print(f"  [{key}] {label}", file=sys.stderr)
        print(f"Choice [{keys}]: ", end="", file=sys.stderr, flush=True)
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return None
        if not line:
            print(file=sys.stderr)
            return None
        if line.strip() in options:
            return line.strip()


def _offer(stop: RelocateStop, *, relocated: bool) -> bool:
    """Ask what to do with a stopped continue. True launches anyway; False relocated and
    the launch should start again from reference resolution. Raises the refusal when
    there is nothing to offer, and exits when the user declines. ``relocated`` says relocate
    already ran for this launch."""
    runtime_root = resolve_project_runtime_root(stop.project_root)
    generations = session_store.chat_generations(runtime_root, stop.chat_id)
    adapter = adapter_for(generations[-1].harness) if generations else None
    if adapter is None:
        raise stop.error
    if stop.refused or relocated:
        # Relocate cannot help again; the harness's own TUI may.
        if not (stop.refused and adapter.tui_recovers_directory):
            raise stop.error
        print(stop.error, file=sys.stderr)
        choice = _ask({"1": f"Launch anyway ({adapter.id} asks for the directory)", "3": "Exit"})
        if choice != "1":
            raise SystemExit(1)
        return True

    old = moved_root(runtime_root, generations, target=stop.project_root)
    if old is None:
        raise stop.error
    here = stop.project_root
    print(stop.error, file=sys.stderr)
    options = {"1": f"Relocate this session (meridian session relocate {stop.chat_id})"}
    if not old.exists():
        options["2"] = f"Relocate all sessions under {old} (to {here})"
    options["3"] = "Don't relocate (exit)"
    choice = _ask(options)
    if choice not in ("1", "2"):
        raise SystemExit(1)
    result = session_relocate_sync(
        SessionRelocateInput(
            ref=stop.chat_id if choice == "1" else None,
            from_root=str(old),
            to_root=str(here),
            project_root=str(here),
        )
    )
    print(result.format_text(), file=sys.stderr)
    return False


def launch_or_offer_relocate(launch: Callable[[bool], _T], *, interactive: bool) -> _T:
    """Run ``launch(launch_anyway)``; when it stops on a moved directory, offer relocate
    (``interactive``) and run it again once from reference resolution."""
    try:
        return launch(False)
    except RelocateStop as stop:
        if not interactive:
            raise stop.error from None
        if _offer(stop, relocated=False):
            return launch(True)
    try:
        return launch(False)
    except RelocateStop as again:
        _offer(again, relocated=True)  # raises unless the user launches anyway
        return launch(True)
