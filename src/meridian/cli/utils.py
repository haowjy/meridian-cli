"""Shared CLI-local parsing and validation helpers."""

import sys
from pathlib import Path
from typing import Literal, overload


def require_established_project_root() -> Path:
    """Resolve project root from GlobalOptions or CWD walk, erroring if none found.

    Reads opts.project_root first (set from -C / --directory flag), then falls
    back to env/CWD discovery via resolve_project_root_resolution. Raises
    SystemExit(1) when the discovery falls back to bare CWD with no project marker.
    """
    from meridian.cli.main import get_global_options
    from meridian.lib.config.project_root import resolve_project_root_resolution

    opts = get_global_options()
    if opts.project_root is not None:
        return opts.project_root
    resolution = resolve_project_root_resolution(execution_cwd=Path.cwd())
    if resolution.source == "cwd":
        print(
            "No Meridian project found. Run from a project directory or pass -C <path>.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return resolution.project_root



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

    if source_ref.startswith("p") and source_ref[1:].isdigit():
        return f"Spawn '{source_ref}' has no recorded session — cannot continue/fork."
    return f"Session '{source_ref}' has no recorded harness session — cannot continue/fork."


def missing_fork_session_error_with_discovery(
    *,
    source_ref: str,
    project_root: Path,
    source_harness: str | None,
    source_chat_id: str | None,
) -> str:
    """Return missing-session error with Pi primary discovery diagnostics when available."""

    normalized_ref = source_ref.strip()
    if (source_harness or "").strip().lower() != "pi":
        return missing_fork_session_error(normalized_ref)

    from meridian.lib.ops.runtime import resolve_runtime_root_for_read
    from meridian.lib.state import spawn_store
    from meridian.lib.state.primary_meta import read_primary_harness_session_discovery

    runtime_root = resolve_runtime_root_for_read(project_root)

    spawn_id: str | None = normalized_ref if normalized_ref.startswith("p") else None
    if spawn_id is None:
        chat_ref = (
            normalized_ref
            if normalized_ref.startswith("c")
            else (source_chat_id or "").strip()
        )
        if chat_ref:
            chat_spawns = spawn_store.list_spawns(runtime_root, filters={"chat_id": chat_ref})
            pi_primary_spawns = [
                row for row in chat_spawns if row.kind == "primary" and row.harness == "pi"
            ]
            if pi_primary_spawns:
                pi_primary_spawns.sort(key=lambda row: row.started_at or "", reverse=True)
                spawn_id = pi_primary_spawns[0].id

    if spawn_id is None:
        return missing_fork_session_error(normalized_ref)

    discovery, detail = read_primary_harness_session_discovery(runtime_root, spawn_id)
    if discovery == "never_created":
        return (
            f"Session '{normalized_ref}' has no Pi session — the original Pi session "
            "was ephemeral or never persisted a session."
        )
    if discovery == "discovery_failed":
        diagnostic = (
            detail.strip()
            if isinstance(detail, str) and detail.strip()
            else "no diagnostic detail"
        )
        return f"Session '{normalized_ref}' could not discover a Pi session — {diagnostic}."
    return missing_fork_session_error(normalized_ref)
