from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.worktree_ensure import WorktreeEnsureError, ensure_work_item_worktree
from meridian.lib.ops.worktree_lifecycle import WorktreeProvisionResult, WorktreeRecoveryResult
from meridian.lib.ops.worktree_ops import resolve_worktree_path
from meridian.lib.state import work_store
from meridian.lib.state.work_store import WorktreeMetadata


def _setup_project(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    roots = resolve_roots(project_root.as_posix())
    return project_root, roots.project_state_dir


def test_ensure_uses_existing_managed_canonical_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "feature-canonical", "", None)

    canonical_path = resolve_worktree_path(project_root, item.name)
    canonical_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(
        project_state_dir,
        item.name,
        path=canonical_path.as_posix(),
        branch=f"feature/{item.name}",
        repo_path=project_root.as_posix(),
        name=item.name,
        pending=False,
        managed=True,
    )

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_ensure._resolve_repo_root",
        lambda _target, *, dry_run: project_root,
    )

    result = ensure_work_item_worktree(
        project_root=project_root,
        project_state_dir=project_state_dir,
        work_id=item.name,
    )

    assert result.status == "already_available"
    assert result.metadata.path == canonical_path.as_posix()


def test_ensure_provisions_when_no_worktree_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "needs-provision", "", None)
    canonical_path = resolve_worktree_path(project_root, item.name)
    branch = f"feature/{item.name}"

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_ensure._resolve_repo_root",
        lambda _target, *, dry_run: project_root,
    )

    def _fake_provision(
        _project_root: Path,
        _work_slug: str,
        _config: object,
        *,
        existing: WorktreeMetadata | None = None,
    ) -> WorktreeProvisionResult:
        assert existing is not None
        assert existing.path == canonical_path.as_posix()
        assert existing.branch == branch
        return WorktreeProvisionResult(
            status="provisioned",
            metadata=WorktreeMetadata(
                path=canonical_path.as_posix(),
                branch=branch,
                repo_path=project_root.as_posix(),
                name=item.name,
                pending=False,
                managed=True,
            ),
            created=True,
        )

    monkeypatch.setattr("meridian.lib.ops.worktree_ensure.provision_for_start", _fake_provision)

    result = ensure_work_item_worktree(
        project_root=project_root,
        project_state_dir=project_state_dir,
        work_id=item.name,
    )

    assert result.status == "provisioned"
    updated = work_store.get_work_item(project_state_dir, item.name)
    assert updated is not None
    assert updated.worktree_path == canonical_path.as_posix()
    assert updated.worktree_managed is True


def test_ensure_recovers_pending_then_provisions_when_recovery_clears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "pending-reprovision", "", None)
    canonical_path = resolve_worktree_path(project_root, item.name)
    branch = f"feature/{item.name}"

    work_store.update_work_item_worktree(
        project_state_dir,
        item.name,
        path=canonical_path.as_posix(),
        branch=branch,
        repo_path=project_root.as_posix(),
        name=item.name,
        pending=True,
        managed=True,
    )

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_ensure._resolve_repo_root",
        lambda _target, *, dry_run: project_root,
    )

    recover_called = {"value": False}

    def _fake_recover(_project_root: Path, _item: object) -> WorktreeRecoveryResult:
        recover_called["value"] = True
        return WorktreeRecoveryResult(
            status="cleared",
            metadata=WorktreeMetadata(
                path=canonical_path.as_posix(),
                branch=branch,
                repo_path=project_root.as_posix(),
                name=item.name,
                pending=False,
                managed=True,
            ),
        )

    monkeypatch.setattr("meridian.lib.ops.worktree_ensure.recover_pending", _fake_recover)

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_ensure.provision_for_start",
        lambda *_args, **_kwargs: WorktreeProvisionResult(
            status="provisioned",
            metadata=WorktreeMetadata(
                path=canonical_path.as_posix(),
                branch=branch,
                repo_path=project_root.as_posix(),
                name=item.name,
                pending=False,
                managed=True,
            ),
            created=True,
        ),
    )

    result = ensure_work_item_worktree(
        project_root=project_root,
        project_state_dir=project_state_dir,
        work_id=item.name,
    )

    assert recover_called["value"] is True
    assert result.status == "recovered"


def test_ensure_manual_missing_errors_without_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "manual-missing", "", None)
    missing = tmp_path / "missing-manual"
    work_store.update_work_item_worktree(
        project_state_dir,
        item.name,
        path=missing.as_posix(),
        branch="feature/manual-missing",
        repo_path=None,
        name=None,
        pending=False,
        managed=False,
    )

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_ensure._resolve_repo_root",
        lambda _target, *, dry_run: project_root,
    )

    with pytest.raises(WorktreeEnsureError, match="manual worktree assignment"):
        ensure_work_item_worktree(
            project_root=project_root,
            project_state_dir=project_state_dir,
            work_id=item.name,
        )


def test_ensure_non_canonical_managed_path_raises_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "drifted", "", None)

    canonical = resolve_worktree_path(project_root, item.name)
    drifted = tmp_path / "repo-drifted-path"
    drifted.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(
        project_state_dir,
        item.name,
        path=drifted.as_posix(),
        branch=f"feature/{item.name}",
        repo_path=project_root.as_posix(),
        name=item.name,
        pending=False,
        managed=True,
    )

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_ensure._resolve_repo_root",
        lambda _target, *, dry_run: project_root,
    )

    with pytest.raises(WorktreeEnsureError, match="non-canonical"):
        ensure_work_item_worktree(
            project_root=project_root,
            project_state_dir=project_state_dir,
            work_id=item.name,
        )

    assert canonical != drifted
