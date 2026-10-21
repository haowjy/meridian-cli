"""Extension-based CLI command registration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from meridian.cli.invocation import Emitter, emit
from meridian.lib.extensions.types import ExtensionCommandSpec, ExtensionSurface

if TYPE_CHECKING:
    from cyclopts import App

    from meridian.lib.extensions.registry import ExtensionCommandRegistry

logger = logging.getLogger(__name__)
HandlerFactory = Callable[[], Callable[..., None]]

# Input model fields injected at runtime, not from user CLI args.
_IMPLICIT_INPUT_FIELDS: frozenset[str] = frozenset({"project_root"})


def _make_auto_handler_from_spec(
    spec: ExtensionCommandSpec,
    emitter: Emitter,
) -> Callable[..., None] | None:
    """Generate a no-arg CLI handler when args_schema has no required fields."""
    sync_handler = spec.sync_handler
    if sync_handler is None:
        return None

    for field_name, field_info in spec.args_schema.model_fields.items():
        if field_info.is_required():
            if field_name in _IMPLICIT_INPUT_FIELDS:
                # Required implicit fields cannot be provided at auto-generation
                # time. Fall back to requiring an explicit handler.
                logger.debug(
                    "Auto-handler skipped for %s: implicit field '%s' is required.",
                    spec.fqid,
                    field_name,
                )
                return None
            return None

    def auto_handler() -> None:
        from meridian.cli.utils import cli_project_root_posix

        payload: dict[str, object] = {}
        if "project_root" in spec.args_schema.model_fields:
            payload["project_root"] = cli_project_root_posix()
        emitter(sync_handler(payload))

    return auto_handler


def register_extension_cli_group(
    app: App,
    *,
    registry: ExtensionCommandRegistry,
    group: str,
    handlers: dict[str, HandlerFactory] | None = None,
    command_help_epilogues: dict[str, str] | None = None,
    default_handler: Callable[..., None] | None = None,
) -> set[str]:
    """Register CLI commands for one extension group.

    Handler keys are fqids (e.g. "meridian.work.start"), not op names. Explicit handlers
    register from metadata; only an auto-handler resolves the operation's schema.
    """
    registered: set[str] = set()
    resolved_handlers = handlers or {}
    resolved_epilogues = command_help_epilogues or {}

    for spec in registry.list_for_cli_group(group):
        if ExtensionSurface.CLI not in spec.surfaces:
            continue
        fqid = spec.fqid
        handler_factory = resolved_handlers.get(fqid)

        if handler_factory is not None:
            handler = handler_factory()
        else:
            resolved = registry.get(fqid)
            auto = _make_auto_handler_from_spec(resolved, emit) if resolved else None
            if auto is None:
                raise ValueError(
                    f"No CLI handler for '{fqid}' and auto-generation failed "
                    f"(args_schema has required fields). Add an explicit handler."
                )
            handler = auto

        handler.__name__ = f"cmd_{spec.cli_group}_{spec.cli_name}"
        epilogue = resolved_epilogues.get(fqid)
        kwargs: dict[str, Any] = {"name": spec.cli_name, "help": spec.summary}
        if epilogue:
            kwargs["help_epilogue"] = epilogue
        app.command(handler, **kwargs)
        registered.add(f"{spec.cli_group}.{spec.cli_name}")

    if default_handler is not None:
        app.default(default_handler)

    return registered


__all__ = [
    "HandlerFactory",
    "register_extension_cli_group",
]
