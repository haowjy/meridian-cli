"""Shared CLI-local parsing and validation helpers."""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, overload

from meridian.lib.config.project_root import ProjectRootSource

NO_ESTABLISHED_PROJECT_MSG = (
    "No Meridian project found. Run from the project root or pass -C <path>."
)


@dataclass(frozen=True)
class CliProjectRoot:
    """CLI project root resolution with established-project semantics."""

    project_root: Path | None
    source: ProjectRootSource
    established: bool


def resolve_cli_project_root() -> CliProjectRoot:
    """Resolve project root from GlobalOptions and env/CWD without raising."""

    from meridian.cli.main import get_global_options
    from meridian.lib.config.project_root import resolve_project_root_resolution

    opts = get_global_options()
    if opts.project_root is not None:
        return CliProjectRoot(
            project_root=opts.project_root,
            source="explicit",
            established=True,
        )
    resolution = resolve_project_root_resolution(execution_cwd=Path.cwd())
    return CliProjectRoot(
        project_root=resolution.project_root,
        source=resolution.source,
        established=True,
    )


def exit_no_established_project() -> None:
    """Exit with the standard no-project error message."""

    print(NO_ESTABLISHED_PROJECT_MSG, file=sys.stderr)
    raise SystemExit(1)


def require_established_project_root() -> Path:
    """Resolve an established project root or exit with a user-facing error."""

    resolution = resolve_cli_project_root()
    if not resolution.established or resolution.project_root is None:
        exit_no_established_project()
    assert resolution.project_root is not None
    return resolution.project_root


def optional_cli_project_root_posix() -> str | None:
    """Return an established project root when explicitly targeted, else None."""

    resolution = resolve_cli_project_root()
    if not resolution.established or resolution.project_root is None:
        return None
    return resolution.project_root.as_posix()


def cli_project_root_posix() -> str:
    """Return the established CLI project root as a POSIX string."""

    return require_established_project_root().as_posix()


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
