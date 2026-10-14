"""CLI handlers for `meridian mermaid` commands."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

from cyclopts import Parameter

from meridian.cli.app_tree import mermaid_app
from meridian.cli.invocation import get_global_options
from meridian.lib.mermaid.validator import ScanOptions, detect_tier, validate_path


@mermaid_app.default
def cmd_mermaid_root() -> None:
    """Show mermaid validation help and active parser tier."""
    tier = detect_tier()
    tier_desc = "@mermaid-js/parser (node)" if tier == "js" else "python heuristics"
    print(
        f"""Mermaid diagram validation.

Active parser: {tier_desc}

Commands:
  meridian mermaid check           Validate mermaid blocks in current directory
  meridian mermaid check path/     Validate mermaid blocks in directory
  meridian mermaid check file.md   Validate mermaid blocks in file
  meridian mermaid check file.mmd  Validate standalone mermaid file

Options:
  --depth N      Limit directory traversal depth
  --exclude PAT  Exclude paths matching glob pattern (repeatable)
  --format FMT   Output format: text (default) or json
  --strict       Treat style warnings as errors
  --no-style     Disable style checks (syntax only)
  --disable CAT  Disable comma-separated warning categories"""
    )
    raise SystemExit(0)


@mermaid_app.command(name="check")
def cmd_mermaid_check(
    path: Annotated[
        Path,
        Parameter(help="File or directory to check (default: cwd)."),
    ] = Path("."),
    *,
    depth: Annotated[
        int | None,
        Parameter(name="--depth", help="Max directory traversal depth."),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        Parameter(name="--exclude", help="Glob pattern to exclude (repeatable)."),
    ] = None,
    fmt: Annotated[
        str,
        Parameter(name="--format", help="Output format: text (default) or json."),
    ] = "text",
    strict: Annotated[
        bool,
        Parameter(name="--strict", help="Treat style warnings as errors (exit 1)."),
    ] = False,
    no_style: Annotated[
        bool,
        Parameter(name="--no-style", help="Disable style checks (syntax only)."),
    ] = False,
    disable: Annotated[
        str | None,
        Parameter(name="--disable", help="Comma-separated warning categories to disable."),
    ] = None,
) -> None:
    """Check mermaid diagram syntax. Exit 0 if clean, exit 1 if errors found."""
    from meridian.lib.mermaid.report import format_check_output
    from meridian.lib.mermaid.scanner import collect_targets
    from meridian.lib.mermaid.serializer import serialize_check
    from meridian.lib.mermaid.style import get_all_categories, run_style_checks
    from meridian.lib.mermaid.style.types import CheckResult, StyleCheckOptions

    resolved = path.resolve()
    if not resolved.exists():
        print(f"Error: path not found: {path}", file=sys.stderr)
        raise SystemExit(2)

    opts = ScanOptions(
        exclude=exclude or [],
        depth=depth,
    )
    result = validate_path(resolved, opts=opts)

    disabled_categories = _parse_disabled_categories(disable)
    known_categories = {category.id for category in get_all_categories()}
    for category in sorted(disabled_categories - known_categories):
        print(f"unknown warning category: {category}", file=sys.stderr)

    style_options = StyleCheckOptions(
        enabled=not no_style,
        strict=strict,
        disabled_categories=disabled_categories,
    )

    root = resolved if resolved.is_dir() else resolved.parent
    targets = collect_targets(
        resolved,
        root,
        depth=opts.depth,
        exclude=opts.exclude if opts.exclude else None,
    )
    active_warnings, suppressed_warnings = run_style_checks(targets, result.results, style_options)
    check_result = CheckResult(
        validation=result,
        warnings=active_warnings,
        suppressed_warnings=suppressed_warnings,
        style_options=style_options,
    )

    effective_fmt = "json" if get_global_options().output.format == "json" else fmt

    if effective_fmt == "json":
        import json

        print(json.dumps(serialize_check(check_result, resolved), indent=2))
    else:
        stdout, stderr = format_check_output(check_result)
        if stdout:
            print(stdout)
        if stderr:
            print(stderr, file=sys.stderr)

    has_errors = check_result.validation.has_errors
    has_warnings = len(check_result.warnings) > 0

    exit_code = 1 if has_errors or (check_result.style_options.strict and has_warnings) else 0

    raise SystemExit(exit_code)


def _parse_disabled_categories(disable: str | None) -> set[str]:
    """Parse comma-separated style warning categories."""
    if not disable:
        return set()
    return {category.strip() for category in disable.split(",") if category.strip()}


__all__ = [
    "cmd_mermaid_check",
    "cmd_mermaid_root",
]
