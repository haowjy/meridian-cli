"""CLI command handlers for the context sync daemon."""

from __future__ import annotations

from typing import Annotated

from cyclopts import App, Parameter

from meridian.cli.agent_help import ALIGNED_PLAIN_FORMATTER
from meridian.cli.help_content import GROUPS
from meridian.cli.invocation import emit


def register_sync_commands(app: App) -> None:
    """Register the ``sync`` subapp."""

    sync_app = App(
        name="sync",
        help=GROUPS["sync"].long_help or GROUPS["sync"].summary,
        help_epilogue="",
        help_formatter=ALIGNED_PLAIN_FORMATTER,
        help_format="plaintext",
    )
    service_app = App(
        name="service",
        help="Install or remove the user service that runs the sync daemon.",
        help_epilogue="",
        help_formatter=ALIGNED_PLAIN_FORMATTER,
        help_format="plaintext",
    )
    sync_app.command(service_app, name="service")
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
        With no daemon running it says so and how to start one: `meridian sync service install`
        where the machine has a service manager, else `meridian sync run`.
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

    def service_install() -> None:
        """Install the sync daemon as a user service and start it.

        A systemd user unit on Linux, a launchd agent on macOS. Works for any install
        and home, and undoes `meridian sync service uninstall`. Meridian also does this
        by itself the first time a person runs it in a project with a git context.
        """

        from meridian.lib.ops.sync_daemon import service_install_sync

        emit(service_install_sync())

    def service_uninstall() -> None:
        """Stop and remove the user service, and keep autosync off on this machine.

        Writes `autosync/service.off` so a later launch doesn't install it again; a
        foreground `meridian sync run` still works. Undo with `meridian sync service install`.
        """

        from meridian.lib.ops.sync_daemon import service_uninstall_sync

        emit(service_uninstall_sync())

    service_app.command(service_install, name="install")
    service_app.command(service_uninstall, name="uninstall")
