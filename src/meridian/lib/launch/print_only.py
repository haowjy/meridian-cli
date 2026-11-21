"""Launches that print the harness command instead of running it.

Meridian may not exec every harness (``HarnessId.launched_by_meridian``): for a
plugin-hosted harness (``agy``) it builds the launch the way it does for any harness and
prints the command. The decision is
made once, at startup, from what argv names: the pinned harness, or the harness of the chat
``--continue`` resumes. Startup uses it to keep the command read-only before anything is
written, and the launch uses it to render the command.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.bundle import PrintedLaunchPorts


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


def refuse_unreachable_options(
    given: Mapping[str, bool], passthrough: tuple[str, ...], ports: PrintedLaunchPorts
) -> None:
    """Refuse the options the harness's printed command cannot honour, each with its reason."""

    problems = [
        f"{flag}: {ports.refused_options[flag]}"
        for flag, present in given.items()
        if present and flag in ports.refused_options
    ]
    problems.extend(
        f"{flag}: {reason}"
        for flag, reason in ports.refused_passthrough.items()
        if any(arg.split("=", 1)[0] == flag for arg in passthrough)
    )
    if problems:
        raise ValueError(
            "a printed launch prints a command and cannot honour:\n  " + "\n  ".join(problems)
        )


__all__ = ["launch_prints", "may_not_exec", "refuse_unreachable_options"]
