"""Static artifact serving over Tailscale."""

from meridian.lib.artifact.store import (
    Serve,
    is_valid_slug,
    load_serves,
    save_serves,
    serves_lock_path,
    serves_path,
)

__all__ = [
    "Serve",
    "is_valid_slug",
    "load_serves",
    "save_serves",
    "serves_lock_path",
    "serves_path",
]
