"""CLI command handlers for workspace operations."""

from collections.abc import Callable

from cyclopts import App

from meridian.cli.ext_registration import register_extension_cli_group
from meridian.lib.extensions.registry import get_first_party_registry


def register_workspace_commands(app: App) -> None:
    # workspace commands have no required CLI args — handlers are auto-generated.
    handlers: dict[str, Callable[[], Callable[..., None]]] = {}
    register_extension_cli_group(
        app,
        registry=get_first_party_registry(),
        group="workspace",
        handlers=handlers,
        command_help_epilogues={
            "meridian.workspace.init": (
                "Create or update local workspace config (meridian.local.toml).\n\n"
                "The file is local-only and scaffolded with commented [workspace.NAME] examples.\n"
                "The command is idempotent and also ensures local gitignore coverage.\n\n"
                "Examples:\n\n"
                "  meridian workspace init\n"
            )
        },
    )
