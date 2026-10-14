"""Shared CLI-local parsing and validation helpers."""

from pathlib import Path
from typing import Literal, overload

from meridian.cli.invocation import get_global_options


def resolve_cli_project_root() -> Path:
    """Resolve the literal CLI project root from global options, env, or CWD."""

    from meridian.lib.config.project_root import resolve_project_root_resolution

    opts = get_global_options()
    if opts.project_root is not None:
        return opts.project_root
    return resolve_project_root_resolution(execution_cwd=Path.cwd()).project_root


def cli_project_root_posix() -> str:
    """Return the CLI project root as a POSIX string."""

    return resolve_cli_project_root().as_posix()


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

    return f"{source_ref} has no verified native session; cannot continue/fork."
