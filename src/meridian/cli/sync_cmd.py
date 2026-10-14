"""CLI command handlers for sync conflict operations."""

from __future__ import annotations

from typing import Annotated

from cyclopts import App, Parameter

from meridian.cli.help_content import GROUPS
from meridian.cli.invocation import emit


def register_sync_commands(app: App) -> None:
    """Register sync conflict CLI commands under a ``sync`` subapp."""

    sync_app = App(
        name="sync",
        help=GROUPS["sync"].long_help or GROUPS["sync"].summary,
        help_epilogue="",
        help_formatter="plain",
        help_format="plaintext",
    )
    conflict_app = App(
        name="conflict",
        help="Manage autosync conflicts.",
        help_epilogue="",
        help_formatter="plain",
        help_format="plaintext",
    )
    sync_app.command(conflict_app, name="conflict")
    app.command(sync_app, name="sync")

    def conflict_list() -> None:
        """List unresolved autosync conflicts across all sync roots."""

        from meridian.lib.ops.sync_conflicts import list_conflicts_sync

        emit(list_conflicts_sync())

    def conflict_show(
        conflict_id: Annotated[
            str,
            Parameter(help="Conflict ID to show details for."),
        ],
    ) -> None:
        """Show detailed information about a specific conflict."""

        from meridian.lib.ops.sync_conflicts import show_conflict_sync

        emit(show_conflict_sync(conflict_id))

    def conflict_resolve(
        conflict_id: Annotated[
            str,
            Parameter(help="Conflict ID to mark as resolved."),
        ],
    ) -> None:
        """Mark a conflict as resolved and clean up metadata/notices."""

        from meridian.lib.ops.sync_conflicts import resolve_conflict_sync

        emit(resolve_conflict_sync(conflict_id))

    conflict_app.command(conflict_list, name="list")
    conflict_app.command(conflict_show, name="show")
    conflict_app.command(conflict_resolve, name="resolve")
