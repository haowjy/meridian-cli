from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.worktree_ensure import (
    WorktreeEnsureError,
    ensure_temporary_worktree,
    ensure_work_item_worktree,
    get_temporary_worktree_status,
)
from meridian.lib.ops.worktree_lifecycle import (
    WorktreeProvisionResult,
    WorktreeRecoveryResult,
    provision_for_start,
)
from meridian.lib.ops.worktree_ops import WorktreeCreateResult, resolve_worktree_path
from meridian.lib.state import temp_worktree_store, work_store
from meridian.lib.state.work_store import WorktreeMetadata


def _setup_project(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    roots = resolve_roots(project_root.as_posix())
    return project_root, roots.project_state_dir


def _mark_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)


def test_provision_for_start_writes_mars_local_guard_for_created_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_path = resolve_worktree_path(repo_root, "guarded-worktree")

    monkeypatch.setattr("meridian.lib.ops.worktree_lifecycle.detect_git_repo", lambda _path: True)
    monkeypatch.setattr(
        "meridian.lib.ops.worktree_lifecycle.resolve_main_repo_root",
        lambda _path: repo_root,
    )

    def _fake_create_worktree(
        _repo_root: Path,
        target_path: Path,
        branch: str,
    ) -> WorktreeCreateResult:
        target_path.mkdir(parents=True)
        return WorktreeCreateResult(path=target_path, branch=branch, created=True)

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_lifecycle.create_worktree",
        _fake_create_worktree,
    )

    result = provision_for_start(repo_root, "guarded-worktree")

    assert result.created is True
    assert (worktree_path / "mars.local.toml").read_text(encoding="utf-8") == (
        "[settings]\ntargets = []\n"
    )


def test_provision_for_start_preserves_existing_mars_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_path = resolve_worktree_path(repo_root, "existing-local")
    existing_content = "[settings]\ntargets = [\".custom\"]\n"

    monkeypatch.setattr("meridian.lib.ops.worktree_lifecycle.detect_git_repo", lambda _path: True)
    monkeypatch.setattr(
        "meridian.lib.ops.worktree_lifecycle.resolve_main_repo_root",
        lambda _path: repo_root,
    )

    def _fake_create_worktree(
        _repo_root: Path,
        target_path: Path,
        branch: str,
    ) -> WorktreeCreateResult:
        target_path.mkdir(parents=True)
        (target_path / "mars.local.toml").write_text(existing_content, encoding="utf-8")
        return WorktreeCreateResult(path=target_path, branch=branch, created=True)

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_lifecycle.create_worktree",
        _fake_create_worktree,
    )

    provision_for_start(repo_root, "existing-local")

    assert (worktree_path / "mars.local.toml").read_text(encoding="utf-8") == existing_content


def test_ensure_temporary_without_repo_uses_execution_cwd_repo(
    tmp_path: Path,
) -> None:
    project_root, _project_state_dir = _setup_project(tmp_path)
    target_repo = tmp_path / "external-target"
    execution_cwd = target_repo / "task"
    _mark_git_repo(project_root)
    _mark_git_repo(target_repo)
    execution_cwd.mkdir()
    (project_root / "meridian.toml").write_text(
        '[workspace.external]\npath = "../external-target"\n',
        encoding="utf-8",
    )

    result = ensure_temporary_worktree(
        project_root=project_root,
        runtime_root=tmp_path / "runtime-root",
        execution_cwd=execution_cwd,
        dry_run=True,
    )

    assert result.status == "temporary_would_provision"
    assert result.repo_root == target_repo.resolve()
    assert result.canonical_path == resolve_worktree_path(target_repo, "temp-default")


def test_ensure_temporary_explicit_repo_wins_over_execution_cwd_repo(
    tmp_path: Path,
) -> None:
    project_root, _project_state_dir = _setup_project(tmp_path)
    target_repo = tmp_path / "external-target"
    execution_cwd = target_repo / "task"
    _mark_git_repo(project_root)
    _mark_git_repo(target_repo)
    execution_cwd.mkdir()

    result = ensure_temporary_worktree(
        project_root=project_root,
        runtime_root=tmp_path / "runtime-root",
        target_repo=project_root.as_posix(),
        execution_cwd=execution_cwd,
        dry_run=True,
    )

    assert result.status == "temporary_would_provision"
    assert result.repo_root == project_root.resolve()
    assert result.canonical_path == resolve_worktree_path(project_root, "temp-default")


def test_ensure_temporary_without_execution_repo_preserves_workspace_ambiguity(
    tmp_path: Path,
) -> None:
    project_root, _project_state_dir = _setup_project(tmp_path)
    target_repo = tmp_path / "external-target"
    _mark_git_repo(project_root)
    _mark_git_repo(target_repo)
    (project_root / "meridian.toml").write_text(
        '[workspace.external]\npath = "../external-target"\n',
        encoding="utf-8",
    )

    with pytest.raises(WorktreeEnsureError, match="Target repository is ambiguous"):
        ensure_temporary_worktree(
            project_root=project_root,
            runtime_root=tmp_path / "runtime-root",
            dry_run=True,
        )


def test_ensure_existing_managed_worktree_does_not_require_repo_selection(
    tmp_path: Path,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    external_repo = tmp_path / "external-target"
    _mark_git_repo(external_repo)
    (project_root / "meridian.toml").write_text(
        '[workspace.external]\npath = "../external-target"\n',
        encoding="utf-8",
    )
    item = work_store.create_work_item(project_state_dir, "existing-managed", "", None)
    managed_path = resolve_worktree_path(project_root, item.name)
    managed_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(
        project_state_dir,
        item.name,
        path=managed_path.as_posix(),
        branch=f"feature/{item.name}",
        repo_path=project_root.as_posix(),
        name=item.name,
        pending=False,
        managed=True,
    )

    result = ensure_work_item_worktree(
        project_root=project_root,
        project_state_dir=project_state_dir,
        work_id=item.name,
        target_repo="unknown-alias",
    )

    assert result.status == "already_available"
    assert result.worktree_path == managed_path.resolve()
    assert result.repo_root == project_root.resolve()


def test_ensure_existing_pending_managed_worktree_heals_without_repo_selection(
    tmp_path: Path,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    external_repo = tmp_path / "external-target"
    _mark_git_repo(external_repo)
    (project_root / "meridian.toml").write_text(
        '[workspace.external]\npath = "../external-target"\n',
        encoding="utf-8",
    )
    item = work_store.create_work_item(project_state_dir, "pending-existing", "", None)
    managed_path = resolve_worktree_path(project_root, item.name)
    managed_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(
        project_state_dir,
        item.name,
        path=managed_path.as_posix(),
        branch=f"feature/{item.name}",
        repo_path=project_root.as_posix(),
        name=item.name,
        pending=True,
        managed=True,
    )

    result = ensure_work_item_worktree(
        project_root=project_root,
        project_state_dir=project_state_dir,
        work_id=item.name,
        target_repo="unknown-alias",
    )

    updated = work_store.get_work_item(project_state_dir, item.name)
    assert result.status == "recovered"
    assert updated is not None
    assert updated.worktree_pending is False
    assert result.repo_root == project_root.resolve()


def test_ensure_existing_manual_worktree_does_not_require_repo_selection(
    tmp_path: Path,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "existing-manual", "", None)
    manual_path = tmp_path / "manual-worktree"
    manual_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(
        project_state_dir,
        item.name,
        path=manual_path.as_posix(),
        pending=False,
        managed=False,
    )

    result = ensure_work_item_worktree(
        project_root=project_root,
        project_state_dir=project_state_dir,
        work_id=item.name,
        target_repo="ambiguous-alias",
    )

    assert result.status == "manual_available"
    assert result.worktree_path == manual_path.resolve()


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


def test_ensure_dry_run_uses_target_repo_without_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    target_repo = tmp_path / "external-target"
    target_repo.mkdir(parents=True, exist_ok=True)
    item = work_store.create_work_item(project_state_dir, "dry-run-target", "", None)
    canonical_path = resolve_worktree_path(target_repo, item.name)

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_ensure._resolve_repo_root",
        lambda _target, *, dry_run: target_repo,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.worktree_ensure.provision_for_start",
        lambda *_args, **_kwargs: pytest.fail("dry-run should not provision a worktree"),
    )

    result = ensure_work_item_worktree(
        project_root=project_root,
        project_state_dir=project_state_dir,
        work_id=item.name,
        target_repo=target_repo.as_posix(),
        dry_run=True,
    )

    assert result.status == "would_provision"
    assert result.repo_root == target_repo.resolve()
    assert result.canonical_path == canonical_path
    assert result.worktree_path == canonical_path
    updated = work_store.get_work_item(project_state_dir, item.name)
    assert updated is not None
    assert updated.worktree_path is None
    assert updated.worktree_managed is False


def test_ensure_missing_work_item_dry_run_can_plan_without_persisting(
    tmp_path: Path,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    _mark_git_repo(project_root)
    canonical_path = resolve_worktree_path(project_root, "new-worktree")

    result = ensure_work_item_worktree(
        project_root=project_root,
        project_state_dir=project_state_dir,
        work_id="new-worktree",
        dry_run=True,
        allow_missing_dry_run=True,
    )

    assert result.status == "would_provision"
    assert result.work_id == "new-worktree"
    assert result.worktree_path == canonical_path
    assert result.metadata.branch == "feature/new-worktree"
    assert "would be created on launch" in (result.warning or "")
    assert work_store.get_work_item(project_state_dir, "new-worktree") is None


def test_ensure_missing_managed_worktree_reprovisions_in_stored_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    target_repo = tmp_path / "external-target"
    target_repo.mkdir(parents=True, exist_ok=True)
    item = work_store.create_work_item(project_state_dir, "managed-repair", "", None)
    missing_path = resolve_worktree_path(target_repo, item.name)
    work_store.update_work_item_worktree(
        project_state_dir,
        item.name,
        path=missing_path.as_posix(),
        branch=f"feature/{item.name}",
        repo_path=target_repo.as_posix(),
        name=item.name,
        pending=False,
        managed=True,
    )
    canonical_path = resolve_worktree_path(target_repo, item.name)

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_ensure._resolve_repo_root",
        lambda target, *, dry_run: target.resolve(),
    )

    def _fake_provision(
        _project_root: Path,
        _work_slug: str,
        *,
        existing: WorktreeMetadata | None = None,
    ) -> WorktreeProvisionResult:
        assert existing is not None
        assert existing.path == canonical_path.as_posix()
        assert existing.branch == f"feature/{item.name}"
        assert existing.repo_path == target_repo.as_posix()
        assert existing.name == item.name
        return WorktreeProvisionResult(
            status="provisioned",
            metadata=WorktreeMetadata(
                path=canonical_path.as_posix(),
                branch=f"feature/{item.name}",
                repo_path=target_repo.as_posix(),
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
        target_repo=project_root.as_posix(),
    )

    assert result.status == "recovered"
    assert result.repo_root == target_repo.resolve()
    assert result.canonical_path == canonical_path
    updated = work_store.get_work_item(project_state_dir, item.name)
    assert updated is not None
    assert updated.worktree_path == canonical_path.as_posix()
    assert updated.worktree_repo_path == target_repo.resolve().as_posix()
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


def test_ensure_temporary_worktree_round_trips_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, _project_state_dir = _setup_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    target_repo = tmp_path / "temp-target"
    target_repo.mkdir(parents=True, exist_ok=True)
    canonical_path = resolve_worktree_path(target_repo, "temp-default")

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_ensure._resolve_repo_root",
        lambda _target, *, dry_run: target_repo,
    )

    def _fake_provision(
        _project_root: Path,
        _work_slug: str,
        *,
        existing: WorktreeMetadata | None = None,
    ) -> WorktreeProvisionResult:
        assert existing is not None
        assert existing.path == canonical_path.as_posix()
        assert existing.branch == "feature/temp-default"
        assert existing.repo_path == target_repo.as_posix()
        assert existing.name == "temp-default"
        return WorktreeProvisionResult(
            status="provisioned",
            metadata=WorktreeMetadata(
                path=canonical_path.as_posix(),
                branch="feature/temp-default",
                repo_path=target_repo.as_posix(),
                name="temp-default",
                pending=False,
                managed=True,
            ),
            created=True,
        )

    monkeypatch.setattr("meridian.lib.ops.worktree_ensure.provision_for_start", _fake_provision)

    result = ensure_temporary_worktree(
        project_root=project_root,
        runtime_root=runtime_root,
        target_repo=target_repo.as_posix(),
    )

    assert result.status == "temporary_provisioned"
    assert result.work_id is None
    assert result.repo_root == target_repo.resolve()
    assert result.canonical_path == canonical_path
    assert result.metadata.name == "temp-default"
    assert result.metadata.path == canonical_path.as_posix()

    stored = temp_worktree_store.get_temporary_worktree(runtime_root, "default")
    assert stored is not None
    assert stored.repo_path == target_repo.resolve().as_posix()
    assert stored.worktree_name == "temp-default"
    assert stored.worktree_path == canonical_path.as_posix()
    assert stored.status == "ready"

    status = get_temporary_worktree_status(runtime_root=runtime_root)
    assert status is not None
    assert status.status == "temporary_available"
    assert status.worktree_path == canonical_path
    assert status.repo_root == target_repo.resolve()


def test_temporary_failure_clears_pending_record_after_provision_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, _project_state_dir = _setup_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    target_repo = tmp_path / "temp-target"
    target_repo.mkdir(parents=True, exist_ok=True)
    canonical_path = resolve_worktree_path(target_repo, "temp-default")

    monkeypatch.setattr(
        "meridian.lib.ops.worktree_ensure._resolve_repo_root",
        lambda _target, *, dry_run: target_repo,
    )

    def _fail_after_pending(*_args: object, **_kwargs: object) -> WorktreeProvisionResult:
        stored = temp_worktree_store.get_temporary_worktree(runtime_root, "default")
        assert stored is not None
        assert stored.status == "pending"
        assert stored.worktree_path == canonical_path.as_posix()
        raise RuntimeError("boom")

    monkeypatch.setattr("meridian.lib.ops.worktree_ensure.provision_for_start", _fail_after_pending)

    with pytest.raises(RuntimeError, match="boom"):
        ensure_temporary_worktree(
            project_root=project_root,
            runtime_root=runtime_root,
            target_repo=target_repo.as_posix(),
        )

    stored = temp_worktree_store.get_temporary_worktree(runtime_root, "default")
    assert stored is None


def test_temporary_status_reports_pending_missing_worktree(tmp_path: Path) -> None:
    _project_root, _project_state_dir = _setup_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    target_repo = tmp_path / "temp-target"
    canonical_path = resolve_worktree_path(target_repo, "temp-default")
    temp_worktree_store.put_temporary_worktree(
        runtime_root,
        key="default",
        repo_path=target_repo.as_posix(),
        worktree_name="temp-default",
        worktree_path=canonical_path.as_posix(),
        branch="feature/temp-default",
        status="pending",
        managed=True,
    )

    status = get_temporary_worktree_status(runtime_root=runtime_root)

    assert status is not None
    assert status.status == "temporary_pending"
    assert status.metadata.pending is True
    assert "interrupted" in (status.warning or "")


def test_temporary_status_heals_pending_existing_worktree(tmp_path: Path) -> None:
    _project_root, _project_state_dir = _setup_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    target_repo = tmp_path / "temp-target"
    canonical_path = resolve_worktree_path(target_repo, "temp-default")
    canonical_path.mkdir(parents=True, exist_ok=True)
    temp_worktree_store.put_temporary_worktree(
        runtime_root,
        key="default",
        repo_path=target_repo.as_posix(),
        worktree_name="temp-default",
        worktree_path=canonical_path.as_posix(),
        branch="feature/temp-default",
        status="pending",
        managed=True,
    )

    status = get_temporary_worktree_status(runtime_root=runtime_root)

    assert status is not None
    assert status.status == "temporary_available"
    assert status.metadata.pending is False
    assert "Recovered interrupted temporary worktree" in (status.warning or "")
    stored = temp_worktree_store.get_temporary_worktree(runtime_root, "default")
    assert stored is not None
    assert stored.status == "ready"


def test_ensure_temporary_heals_pending_existing_worktree(
    tmp_path: Path,
) -> None:
    project_root, _project_state_dir = _setup_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    target_repo = tmp_path / "temp-target"
    subprocess.run(["git", "init", str(target_repo)], check=True, capture_output=True, text=True)
    canonical_path = resolve_worktree_path(target_repo, "temp-default")
    canonical_path.mkdir(parents=True, exist_ok=True)
    temp_worktree_store.put_temporary_worktree(
        runtime_root,
        key="default",
        repo_path=target_repo.as_posix(),
        worktree_name="temp-default",
        worktree_path=canonical_path.as_posix(),
        branch="feature/temp-default",
        status="pending",
        managed=True,
    )
    result = ensure_temporary_worktree(
        project_root=project_root,
        runtime_root=runtime_root,
    )

    assert result.status == "temporary_available"
    assert result.metadata.pending is False
    assert result.worktree_path == canonical_path
    assert "Recovered interrupted temporary worktree" in (result.warning or "")
    stored = temp_worktree_store.get_temporary_worktree(runtime_root, "default")
    assert stored is not None
    assert stored.status == "ready"


def test_ensure_work_item_fails_for_non_git_target(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "plain-target", "", None)

    with pytest.raises(WorktreeEnsureError, match="not inside a git repository"):
        ensure_work_item_worktree(
            project_root=project_root,
            project_state_dir=project_state_dir,
            work_id=item.name,
            target_repo=project_root.as_posix(),
        )

    updated = work_store.get_work_item(project_state_dir, item.name)
    assert updated is not None
    assert updated.worktree_path is None
    assert updated.worktree_pending is False


def test_ensure_temporary_fails_for_non_git_target(tmp_path: Path) -> None:
    project_root, _project_state_dir = _setup_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"

    with pytest.raises(WorktreeEnsureError, match="not inside a git repository"):
        ensure_temporary_worktree(
            project_root=project_root,
            runtime_root=runtime_root,
            target_repo=project_root.as_posix(),
        )

    assert temp_worktree_store.get_temporary_worktree(runtime_root, "default") is None


def test_temporary_record_with_non_canonical_path_errors(tmp_path: Path) -> None:
    project_root, _project_state_dir = _setup_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    target_repo = tmp_path / "temp-target"
    _mark_git_repo(target_repo)
    non_canonical_path = tmp_path / "custom-temp-path"
    non_canonical_path.mkdir(parents=True, exist_ok=True)
    temp_worktree_store.put_temporary_worktree(
        runtime_root,
        key="default",
        repo_path=target_repo.as_posix(),
        worktree_name="temp-default",
        worktree_path=non_canonical_path.as_posix(),
        branch="feature/temp-default",
        status="ready",
        managed=True,
    )

    with pytest.raises(WorktreeEnsureError, match="non-canonical"):
        ensure_temporary_worktree(
            project_root=project_root,
            runtime_root=runtime_root,
            dry_run=True,
        )


def test_temporary_record_rejects_different_requested_repo(tmp_path: Path) -> None:
    project_root, _project_state_dir = _setup_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _mark_git_repo(repo_a)
    _mark_git_repo(repo_b)
    canonical_path = resolve_worktree_path(repo_a, "temp-default")
    temp_worktree_store.put_temporary_worktree(
        runtime_root,
        key="default",
        repo_path=repo_a.as_posix(),
        worktree_name="temp-default",
        worktree_path=canonical_path.as_posix(),
        branch="feature/temp-default",
        status="ready",
        managed=True,
    )

    with pytest.raises(WorktreeEnsureError, match="different target repository"):
        ensure_temporary_worktree(
            project_root=project_root,
            runtime_root=runtime_root,
            target_repo=repo_b.as_posix(),
            dry_run=True,
        )


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
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "drifted", "", None)

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

    with pytest.raises(WorktreeEnsureError, match="non-canonical"):
        ensure_work_item_worktree(
            project_root=project_root,
            project_state_dir=project_state_dir,
            work_id=item.name,
        )


def test_ensure_legacy_managed_metadata_without_canonical_fields_errors(
    tmp_path: Path,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "legacy-managed", "", None)
    legacy_path = tmp_path / "legacy-managed-path"
    legacy_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(
        project_state_dir,
        item.name,
        path=legacy_path.as_posix(),
        branch=f"feature/{item.name}",
        repo_path=None,
        name=None,
        pending=False,
        managed=True,
    )

    with pytest.raises(WorktreeEnsureError, match="missing canonical repo/name"):
        ensure_work_item_worktree(
            project_root=project_root,
            project_state_dir=project_state_dir,
            work_id=item.name,
        )
