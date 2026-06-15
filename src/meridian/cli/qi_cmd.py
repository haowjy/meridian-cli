"""CLI handlers for `meridian qi` commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from cyclopts import Parameter

from meridian.cli._cmd_utils import resolve_fmt
from meridian.cli.app_tree import qi_app

if TYPE_CHECKING:
    from meridian.lib.ops.qi import QiShowOutput


def _paths_or_default(paths: tuple[Path, ...]) -> list[Path]:
    return list(paths) if paths else [Path(".")]


def _collect_qi_graph_results(
    paths: list[Path],
    project_root: Path,
) -> list[QiShowOutput]:
    """Resolve qi graph output for each path, deduped by boundary_path."""
    from meridian.lib.ops.qi import qi_show_sync

    results: list[QiShowOutput] = []
    seen_boundaries: set[str] = set()
    for path in paths:
        result = qi_show_sync(path.resolve(), project_root)
        if result.boundary_path in seen_boundaries:
            continue
        seen_boundaries.add(result.boundary_path)
        results.append(result)
    return results


def _format_qi_graph_text(results: list[QiShowOutput]) -> str:
    """Format one or more qi graph results for text output."""
    if not results:
        return ""
    if len(results) == 1:
        return results[0].format_text()

    parts: list[str] = []
    for result in results:
        parts.append(f"# {result.boundary_path}")
        parts.append("")
        parts.append(result.format_text())
    return "\n\n".join(parts)


@qi_app.default
def cmd_qi_root(
    path: Annotated[
        Path,
        Parameter(help="Directory to summarize (default: cwd)."),
    ] = Path("."),
) -> None:
    """Quick summary of inline knowledge coverage for a directory."""
    from meridian.lib.ops.qi import qi_summary_sync

    resolved = path.resolve()
    if not resolved.exists():
        print(f"Error: path not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    if not resolved.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        raise SystemExit(2)

    result = qi_summary_sync(resolved)
    print(result.format_text())
    raise SystemExit(0)


@qi_app.command(name="graph")
def cmd_qi_graph(
    *paths: Annotated[
        Path,
        Parameter(help="File or directory to inspect (default: cwd)."),
    ],
    fmt: Annotated[
        str,
        Parameter(name="--format", help="Output format: text (default) or json."),
    ] = "text",
) -> None:
    """Show inline knowledge boundary for a path."""
    from meridian.cli.utils import require_established_project_root

    for path in _paths_or_default(paths):
        resolved = path.resolve()
        if not resolved.exists():
            print(f"Error: path not found: {path}", file=sys.stderr)
            raise SystemExit(2)

    project_root = require_established_project_root()
    resolved_paths = [path.resolve() for path in _paths_or_default(paths)]
    results = _collect_qi_graph_results(resolved_paths, project_root)

    if resolve_fmt(fmt) == "json":
        print(json.dumps([result.model_dump() for result in results], indent=2))
    else:
        print(_format_qi_graph_text(results))

    raise SystemExit(0)


@qi_app.command(name="check")
def cmd_qi_check(
    path: Annotated[
        Path,
        Parameter(help="Directory to check (default: cwd)."),
    ] = Path("."),
    *,
    fmt: Annotated[
        str,
        Parameter(name="--format", help="Output format: text (default) or json."),
    ] = "text",
) -> None:
    """Check inline knowledge health."""
    from meridian.lib.ops.qi import qi_check_sync

    resolved = path.resolve()
    if not resolved.exists():
        print(f"Error: path not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    if not resolved.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        raise SystemExit(2)

    result = qi_check_sync(resolved)

    if resolve_fmt(fmt) == "json":
        print(json.dumps(result.model_dump(), indent=2))
    else:
        print(result.format_text())

    raise SystemExit(1 if result.has_errors else 0)


@qi_app.command(name="explore")
def cmd_qi_explore(
    *paths: Annotated[
        Path,
        Parameter(help="Directory to explore (default: cwd)."),
    ],
    port: Annotated[
        int,
        Parameter(help="Port to bind; 0 auto-assigns."),
    ] = 0,
    no_open: Annotated[
        bool,
        Parameter(name="--no-open", help="Do not open a browser tab."),
    ] = False,
) -> None:
    """Start a local web UI to explore qi-layer boundaries."""
    import webbrowser

    from meridian.qi_explorer.discovery import discover_scan_roots
    from meridian.qi_explorer.server import ExplorerState, create_server

    paths_or_default = _paths_or_default(paths)
    for path in paths_or_default:
        resolved = path.resolve()
        if not resolved.exists():
            print(f"Error: path not found: {path}", file=sys.stderr)
            raise SystemExit(2)
        if not resolved.is_dir():
            print(f"Error: not a directory: {path}", file=sys.stderr)
            raise SystemExit(2)
    if port < 0 or port > 65535:
        print("Error: port must be between 0 and 65535", file=sys.stderr)
        raise SystemExit(2)

    discovery = discover_scan_roots([path.resolve() for path in paths_or_default])
    state = ExplorerState(discovery)
    primary_count = sum(1 for root in discovery.roots if root.kind == "primary")
    context_count = sum(1 for root in discovery.roots if root.kind == "context")
    extra_primary_count = max(0, primary_count - 1)

    httpd = create_server(discovery, port=port, state=state)
    bound_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{bound_port}"

    if extra_primary_count:
        roots_note = f"+{extra_primary_count} more roots, +{context_count} context roots"
    else:
        roots_note = f"+{context_count} context roots"

    print(
        f"qi explore serving {discovery.primary} ({roots_note}) at {url}",
        flush=True,
    )
    if state.is_empty:
        print("Notice: no qi-layer nodes found in scan roots.", flush=True)

    if not no_open:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nqi explore stopped.", flush=True)
        httpd.server_close()
        raise SystemExit(0) from None


__all__ = [
    "cmd_qi_check",
    "cmd_qi_explore",
    "cmd_qi_graph",
    "cmd_qi_root",
]
