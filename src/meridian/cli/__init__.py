"""CLI package for meridian.

Keep package import startup-cheap: loading ``meridian.cli`` must not import the
full Cyclopts command tree.  Attribute access preserves the historical
``meridian.cli.app`` / ``meridian.cli.main`` exports for callers that need them.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cyclopts import App

    app: App


def __getattr__(name: str) -> Any:
    if name == "app":
        cli_main = importlib.import_module("meridian.cli.main")

        return getattr(cli_main, name)
    raise AttributeError(name)


def main(*args: Any, **kwargs: Any) -> Any:
    """Lazily delegate to the full CLI entry point."""

    cli_main = importlib.import_module("meridian.cli.main")
    return cli_main.main(*args, **kwargs)


__all__ = ["app", "main"]
