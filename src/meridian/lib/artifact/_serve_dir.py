"""Prefix-aware static server used by detached artifact serves."""

from __future__ import annotations

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from meridian.lib.artifact.store import is_valid_slug


def main() -> None:
    """Serve a directory under one URL path prefix."""
    if len(sys.argv) != 4:
        raise SystemExit("usage: _serve_dir DIRECTORY PORT SLUG")
    directory = Path(sys.argv[1])
    try:
        port = int(sys.argv[2])
    except ValueError as exc:
        raise SystemExit("port must be an integer") from exc
    slug = sys.argv[3]
    if not directory.is_dir():
        raise SystemExit(f"not a directory: {directory}")
    if not 1 <= port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    if not is_valid_slug(slug):
        raise SystemExit("invalid slug")

    prefix = f"/{slug}"

    class PrefixHandler(SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            if path == prefix:
                path = "/"
            elif path.startswith(f"{prefix}/"):
                path = path[len(prefix) :]
            return super().translate_path(path)

    handler = partial(PrefixHandler, directory=str(directory))
    ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()


if __name__ == "__main__":
    main()
