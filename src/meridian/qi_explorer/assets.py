"""Locate the packaged qi explore frontend entrypoint."""

from __future__ import annotations

import importlib.resources
from pathlib import Path


def resolve_index_html() -> Path:
    """Return the path to ``index.html`` for qi explore."""

    package_file = Path(__file__).resolve().parent / "index.html"
    if package_file.is_file():
        return package_file

    ref = importlib.resources.files("meridian.qi_explorer") / "index.html"
    with importlib.resources.as_file(ref) as extracted:
        return Path(extracted)


__all__ = ["resolve_index_html"]
