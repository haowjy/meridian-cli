"""Shared CLI-local parsing and validation helpers."""

from pathlib import Path
from typing import Literal, overload


def require_established_project_root() -> Path:
    """Resolve project root from GlobalOptions or CWD walk, erroring if none found.

    Reads opts.project_root first (set from --project-root flag), then falls
    back to env/CWD discovery via resolve_project_root_resolution. Raises
    SystemExit when the discovery falls back to bare CWD with no project marker.
    """
    from meridian.cli.main import get_global_options
    from meridian.lib.config.project_root import resolve_project_root_resolution

    opts = get_global_options()
    if opts.project_root is not None:
        return opts.project_root
    resolution = resolve_project_root_resolution(execution_cwd=Path.cwd())
    if resolution.source == "cwd":
        raise SystemExit(
            "No Meridian project found. Run from a project directory or pass --project-root."
        )
    return resolution.project_root


@overload
def parse_csv_list(
    raw: str | None,
    *,
    field_name: str,
    none_for_empty: Literal[False] = False,
) -> tuple[str, ...]: ...


@overload
def parse_csv_list(
    raw: str | None,
    *,
    field_name: str,
    none_for_empty: Literal[True],
) -> tuple[str, ...] | None: ...


def parse_csv_list(
    raw: str | None,
    *,
    field_name: str,
    none_for_empty: bool = False,
) -> tuple[str, ...] | None:
    """Parse comma-separated values into a normalized tuple."""

    if raw is None:
        return None if none_for_empty else ()

    trimmed = raw.strip()
    if not trimmed:
        return None if none_for_empty else ()

    parts = [part.strip() for part in trimmed.split(",")]
    if any(not part for part in parts):
        raise ValueError(
            f"Invalid value for '{field_name}': expected comma-separated non-empty names."
        )
    return tuple(parts)


def missing_fork_session_error(source_ref: str) -> str:
    """Return a consistent missing-session error for fork/continue flows."""

    if source_ref.startswith("p") and source_ref[1:].isdigit():
        return f"Spawn '{source_ref}' has no recorded session — cannot continue/fork."
    return f"Session '{source_ref}' has no recorded harness session — cannot continue/fork."
