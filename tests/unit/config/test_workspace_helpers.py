from pathlib import Path

import pytest

from meridian.lib.config.workspace import (
    ResolvedWorkspaceRoot,
    WorkspaceEntryConfig,
    WorkspaceFinding,
    WorkspaceSnapshot,
    _evaluate_named_workspace_config,
    _parse_workspace_entry,
    _unknown_workspace_key_findings,
    get_projectable_roots,
)


def test_workspace_snapshot_invalid_normalizes_blank_messages() -> None:
    snapshot = WorkspaceSnapshot.invalid(path=Path("/tmp/meridian.local.toml"), message="   ")

    assert snapshot.status == "invalid"
    assert snapshot.source_paths == (Path("/tmp/meridian.local.toml"),)
    assert snapshot.findings == (
        WorkspaceFinding(
            code="workspace_invalid",
            message="Workspace file is invalid.",
            payload={"path": "/tmp/meridian.local.toml"},
        ),
    )


@pytest.mark.parametrize("name", ["Alpha", "1root", "bad.dot", "space name"])
def test_parse_workspace_entry_rejects_invalid_entry_names(name: str) -> None:
    source_path = Path("/tmp/meridian.toml")

    with pytest.raises(ValueError, match=r"must match \^\[a-z\]\[a-z0-9_-\]\*\$"):
        _parse_workspace_entry(
            name=name,
            raw_entry={"path": "./repo"},
            source_path=source_path,
        )


@pytest.mark.parametrize(
    ("raw_entry", "expected"),
    [
        ({}, "is required"),
        ({"path": ""}, "must be non-empty"),
        ({"path": "   \t  "}, "must be non-empty"),
    ],
)
def test_parse_workspace_entry_rejects_missing_and_blank_paths(
    raw_entry: object,
    expected: str,
) -> None:
    with pytest.raises(ValueError, match=expected):
        _parse_workspace_entry(
            name="docs",
            raw_entry=raw_entry,
            source_path=Path("/tmp/meridian.local.toml"),
        )


def test_unknown_workspace_key_findings_collects_sorted_payload_keys() -> None:
    finding = _unknown_workspace_key_findings(
        entries_by_path=[
            (
                Path("/tmp/meridian.toml"),
                {
                    "alpha": WorkspaceEntryConfig(
                        path="./alpha",
                        extra_keys={"zeta": True, "alpha": False},
                    ),
                },
            ),
            (
                Path("/tmp/meridian.local.toml"),
                {
                    "beta": WorkspaceEntryConfig(path="./beta", extra_keys={"note": "x"}),
                },
            ),
        ]
    )[0]

    assert finding.code == "workspace_unknown_key"
    assert finding.payload == {
        "keys": [
            "workspace.alpha.alpha",
            "workspace.alpha.zeta",
            "workspace.beta.note",
        ]
    }
    assert finding.message == (
        "Workspace config contains unknown keys: "
        "workspace.alpha.alpha, workspace.alpha.zeta, workspace.beta.note."
    )


def test_evaluate_named_workspace_config_preserves_merge_and_projection_order(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    committed_root = project_root / "committed"
    merged_root = project_root / "merged-local"
    committed_root.mkdir()
    merged_root.mkdir()

    snapshot = _evaluate_named_workspace_config(
        project_root=project_root,
        committed_path=project_root / "meridian.toml",
        local_path=project_root / "meridian.local.toml",
        committed_entries={
            "alpha": WorkspaceEntryConfig(path="./committed"),
            "beta": WorkspaceEntryConfig(path="./committed-beta"),
        },
        local_entries={
            "beta": WorkspaceEntryConfig(path="./merged-local"),
            "gamma": WorkspaceEntryConfig(path="./missing-local"),
        },
        source_paths=(project_root / "meridian.toml", project_root / "meridian.local.toml"),
        initial_findings=(
            WorkspaceFinding(
                code="workspace_unknown_key",
                message="unknown keys",
                payload={"keys": ["workspace.beta.note"]},
            ),
        ),
    )

    assert snapshot.status == "present"
    assert [root.name for root in snapshot.roots] == ["alpha", "beta", "gamma"]
    assert [root.source for root in snapshot.roots] == ["committed", "merged", "local"]
    assert [root.declared_path for root in snapshot.roots] == [
        "./committed",
        "./merged-local",
        "./missing-local",
    ]
    assert get_projectable_roots(snapshot) == (committed_root.resolve(), merged_root.resolve())
    assert snapshot.findings[0].code == "workspace_unknown_key"
    assert snapshot.findings[1] == WorkspaceFinding(
        code="workspace_local_missing_root",
        message=(
            "Local workspace root 'gamma' does not exist: "
            f"{(project_root / 'missing-local').resolve().as_posix()}."
        ),
        payload={
            "name": "gamma",
            "path": (project_root / "missing-local").resolve().as_posix(),
        },
    )


def test_get_projectable_roots_skips_disabled_and_missing_entries(tmp_path: Path) -> None:
    existing = (tmp_path / "existing").resolve()
    existing.mkdir()

    snapshot = WorkspaceSnapshot(
        status="present",
        roots=(
            ResolvedWorkspaceRoot(
                name="projected",
                declared_path="./existing",
                resolved_path=existing,
                enabled=True,
                exists=True,
                source="committed",
            ),
            ResolvedWorkspaceRoot(
                name="disabled",
                declared_path="./disabled",
                resolved_path=(tmp_path / "disabled").resolve(),
                enabled=False,
                exists=True,
                source="local",
            ),
            ResolvedWorkspaceRoot(
                name="missing",
                declared_path="./missing",
                resolved_path=(tmp_path / "missing").resolve(),
                enabled=True,
                exists=False,
                source="merged",
            ),
        ),
    )

    assert get_projectable_roots(snapshot) == (existing,)
