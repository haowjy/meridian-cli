"""CLI command handlers for the context sync daemon and autosync conflicts."""

from __future__ import annotations

from typing import Annotated

from cyclopts import App, Parameter

from meridian.cli.agent_help import ALIGNED_PLAIN_FORMATTER
from meridian.cli.help_content import GROUPS
from meridian.cli.invocation import emit


def register_sync_commands(app: App) -> None:
    """Register sync conflict CLI commands under a ``sync`` subapp."""

    sync_app = App(
        name="sync",
        help=GROUPS["sync"].long_help or GROUPS["sync"].summary,
        help_epilogue="",
        help_formatter=ALIGNED_PLAIN_FORMATTER,
        help_format="plaintext",
    )
    conflict_app = App(
        name="conflict",
        help="Manage autosync conflicts.",
        help_epilogue="",
        help_formatter=ALIGNED_PLAIN_FORMATTER,
        help_format="plaintext",
    )
    sync_app.command(conflict_app, name="conflict")
    app.command(sync_app, name="sync")

    def sync_run() -> None:
        """Run the sync daemon in the foreground until it is stopped.

        Keeps every git context's clone in sync with its remote: commits local edits,
        fetches, merges and pushes. Restarts itself after an upgrade. Exits 1 with
        "already running (pid N)" when a daemon holds the lock. Stop it with Ctrl-C.
        """

        from meridian.lib.ops.sync_daemon import run_daemon_sync

        raise SystemExit(run_daemon_sync())

    def sync_now(
        context: Annotated[
            str | None,
            Parameter(
                help="Context name (work, kb, or a named context). Default: all git contexts."
            ),
        ] = None,
    ) -> None:
        """Sync this project's git contexts now and report each clone's state.

        Asks the running daemon for a pass and waits up to 60 s for it, so an edit made
        just before is included. Exits non-zero unless every clone ended `synced`.
        With no daemon running it says so; start one with `meridian sync run`.
        """

        from meridian.lib.ops.sync_daemon import sync_now_sync

        result = sync_now_sync(context)
        emit(result)
        if not result.ok:
            raise SystemExit(1)

    def sync_status() -> None:
        """Show the sync daemon and each of this project's git contexts.

        Per context: state (synced, retrying, stopped, pending, off), the running step and
        since when, branch, ahead and behind, watch mode, last success, and the reason and
        fix when stopped. A daemon that isn't running, or a state file this meridian can't
        read, shows as `pending` with the cause in `note`. `--json` prints the same for
        scripts, plus the daemon's `alive_at` and any skipped records.
        """

        from meridian.lib.ops.sync_daemon import sync_status_sync

        emit(sync_status_sync())

    sync_app.command(sync_run, name="run")
    sync_app.command(sync_now, name="now")
    sync_app.command(sync_status, name="status")

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
