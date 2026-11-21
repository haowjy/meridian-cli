"""Launches that print the harness command instead of running it.

Meridian may not exec every harness (``HarnessId.launched_by_meridian``): for ``agy`` it
builds the launch the way it does for any harness and prints the command. The decision is
made once, at startup, from what argv names: the pinned harness, or the harness of the chat
``--continue`` resumes. Startup uses it to keep the command read-only before anything is
written, and the launch uses it to render the command.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from meridian.lib.core.types import HarnessId


def may_not_exec(harness: str | None) -> bool:
    """Whether ``harness`` names a harness Meridian builds a command for and never runs."""

    try:
        return harness is not None and not HarnessId(harness).launched_by_meridian
    except ValueError:
        return False


def launch_prints(*, harness: str | None, continue_ref: str | None, project_root: Path) -> bool:
    """Whether a primary launch with these arguments prints its command.

    ``--continue`` is resolved read-only. A reference that does not resolve prints nothing
    here; the launch resolves it again and refuses with the real reason.
    """

    if harness is not None:
        return may_not_exec(harness)
    if continue_ref is None or not continue_ref.strip():
        return False
    from meridian.lib.ops.reference import resolve_session_reference

    try:
        return may_not_exec(resolve_session_reference(project_root, continue_ref.strip()).harness)
    except ValueError:
        return False


# What the person's options would change, and why it cannot reach a session Meridian does not
# start: the plugin's hook and server see the conversation only after the person runs the
# command, and a command line has no place for these.
_REFUSED_OPTIONS = {
    "--fork": "a fork starts a conversation Meridian would have to create",
    "--fork-fresh": "a fork starts a conversation Meridian would have to create",
    "--from": "the printed command carries only your own prompt",
    "-f": "the printed command carries only your own prompt",
    "--skills": "agy loads skills from the project's synced plugin, not from the command",
    "--goal": "a goal is a completion contract Meridian's runner enforces",
    "--sandbox": "agy has no sandbox flag Meridian can set",
    "--network": "agy has no network flag Meridian can set",
    "--network-allow": "agy has no network flag Meridian can set",
    "--network-deny": "agy has no network flag Meridian can set",
    "--autocompact": "agy has no compaction flag",
    "--autocompact-pct": "agy has no compaction flag",
    "--timeout": "Meridian does not run agy, so it cannot stop it",
}


def refuse_unreachable_options(given: Mapping[str, bool], passthrough: tuple[str, ...]) -> None:
    """Refuse the options a printed launch cannot honour, each with its reason."""

    problems = [f"{flag}: {_REFUSED_OPTIONS[flag]}" for flag, present in given.items() if present]
    if any(arg.split("=", 1)[0] == "--append-system-prompt" for arg in passthrough):
        problems.append("--append-system-prompt: agy has no system-prompt flag to carry it")
    if problems:
        raise ValueError(
            "agy launches print a command and cannot honour:\n  " + "\n  ".join(problems)
        )


__all__ = ["launch_prints", "may_not_exec", "refuse_unreachable_options"]
