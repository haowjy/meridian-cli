"""Startup-cheap Cyclopts app assembly from command descriptors."""

from __future__ import annotations

from cyclopts import App

from meridian import __version__
from meridian.cli.startup.catalog import COMMAND_CATALOG, CommandDescriptor
from meridian.cli.startup.lazy_dispatch import make_lazy_command


def _command_summary(path: tuple[str, ...]) -> str:
    descriptor = COMMAND_CATALOG.get(path)
    if descriptor is not None:
        return descriptor.summary
    return path[-1] if path else "Multi-agent orchestration across Claude, Codex, and OpenCode."


def _path_has_children(
    path: tuple[str, ...],
    descriptors: tuple[CommandDescriptor, ...],
) -> bool:
    return any(
        len(descriptor.command_path) > len(path)
        and descriptor.command_path[: len(path)] == path
        for descriptor in descriptors
    )


def build_lazy_app() -> App:
    """Build the root Cyclopts App with lazy command registrations from the catalog."""

    descriptors = tuple(COMMAND_CATALOG.all_descriptors())
    root = App(
        name="meridian",
        help="Multi-agent orchestration across Claude, Codex, and OpenCode.",
        version=__version__,
        help_formatter="plain",
    )
    apps_by_path: dict[tuple[str, ...], App] = {(): root}

    for descriptor in descriptors:
        path = descriptor.command_path
        if not path:
            continue

        for prefix_length in range(1, len(path)):
            prefix = path[:prefix_length]
            if prefix in apps_by_path:
                continue
            parent_path = prefix[:-1]
            parent = apps_by_path[parent_path]
            sub_app = App(
                name=prefix[-1],
                help=_command_summary(prefix),
                help_formatter="plain",
            )
            parent.command(sub_app, name=prefix[-1])
            apps_by_path[prefix] = sub_app

        if _path_has_children(path, descriptors):
            if path in apps_by_path:
                continue
            parent = apps_by_path[path[:-1]]
            sub_app = App(
                name=path[-1],
                help=descriptor.summary,
                help_formatter="plain",
            )
            parent.command(sub_app, name=path[-1])
            apps_by_path[path] = sub_app
            continue
        parent = apps_by_path[path[:-1]]
        handler = make_lazy_command(descriptor.lazy_target)
        parent.command(handler, name=path[-1], help=descriptor.summary)

    return root


__all__ = ["build_lazy_app"]
