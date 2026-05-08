"""Launch-time workspace policy gates."""

from pathlib import Path

from meridian.lib.config.workspace import WorkspaceSnapshot, resolve_workspace_snapshot


def _format_workspace_source_path(*, project_root: Path, source_paths: tuple[Path, ...]) -> str:
    if not source_paths:
        return "meridian.toml or meridian.local.toml"
    labels: list[str] = []
    for path in source_paths:
        try:
            labels.append(path.relative_to(project_root).as_posix())
        except ValueError:
            labels.append(path.as_posix())
    return ", ".join(labels)


def resolve_workspace_snapshot_for_launch(project_root: Path) -> WorkspaceSnapshot:
    """Resolve launch workspace snapshot and raise on invalid topology."""

    snapshot = resolve_workspace_snapshot(project_root)
    if snapshot.status != "invalid":
        return snapshot
    details = "; ".join(finding.message for finding in snapshot.findings if finding.message.strip())
    if not details:
        details = "Workspace file is invalid."
    path = _format_workspace_source_path(
        project_root=project_root.resolve(),
        source_paths=snapshot.source_paths,
    )
    raise ValueError(
        f"Invalid workspace config in {path}. {details} "
        "Run `meridian config show` or `meridian doctor` for details."
    )


def ensure_workspace_valid_for_launch(project_root: Path) -> None:
    """Raise when workspace topology is invalid for launch-time commands."""

    resolve_workspace_snapshot_for_launch(project_root)


__all__ = ["ensure_workspace_valid_for_launch", "resolve_workspace_snapshot_for_launch"]
