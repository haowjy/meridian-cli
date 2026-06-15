"""Top-level `meridian mktemp` command."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from cyclopts import Parameter

if TYPE_CHECKING:
    from cyclopts import App

Emitter = Callable[[Any], None]


def register_mktemp_command(app: App, emit: Emitter) -> None:
    @app.command(name="mktemp")
    def mktemp(  # pyright: ignore[reportUnusedFunction]
        suffix: Annotated[
            str,
            Parameter(
                name="--suffix",
                help='Filename suffix (default: ".md").',
            ),
        ] = ".md",
    ) -> None:
        """Create a temp file and print its absolute path."""

        fd, path = tempfile.mkstemp(suffix=suffix, prefix="meridian-")
        os.close(fd)
        emit(Path(path).resolve().as_posix())


__all__ = ["register_mktemp_command"]
