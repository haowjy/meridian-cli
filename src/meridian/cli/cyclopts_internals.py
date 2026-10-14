"""Typed boundary for the Cyclopts internals used by CLI help rendering.

Verified against Cyclopts 4.10.2. Recheck these adapters when upgrading
Cyclopts; its public API does not currently expose either operation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cyclopts import App, Group
    from cyclopts.help import HelpPanel


def assemble_help_panels(
    app: App,
    tokens: list[str],
    help_format: str,
) -> list[tuple[Group | None, HelpPanel]]:
    """Assemble Cyclopts help panels through its typed private method."""

    return app._assemble_help_panels(  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]  # Cyclopts has no public or fully typed panel-assembly API.
        tokens,
        help_format,
    )


def replace_group_show(group: Group, show: bool | None) -> bool | None:
    """Set Cyclopts' raw group visibility and return its previous value."""

    previous: bool | None = group._show  # pyright: ignore[reportPrivateUsage]  # Group.show is read-only and loses the raw None state.
    object.__setattr__(group, "_show", show)
    return previous


__all__ = ["assemble_help_panels", "replace_group_show"]
