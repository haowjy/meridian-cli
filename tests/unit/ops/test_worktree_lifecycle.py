from pathlib import Path

from meridian.lib.config.settings import WorkConfig
from meridian.lib.ops import worktree_lifecycle as lifecycle
from meridian.lib.ops.worktree_ops import WorktreeCreateResult
from meridian.lib.state.work_store import WorkItem, WorktreeMetadata


def test_restore_for_reopen_uses_persisted_branch(monkeypatch, tmp_path: Path) -> None:
    requested: dict[str, str] = {}
    missing_path = tmp_path / "repo.worktrees" / "renamed-item"

    def fake_resolve_main_repo_root(project_root: Path) -> Path:
        return project_root

    def fake_create_worktree(
        repo_root: Path, target_path: Path, branch: str
    ) -> WorktreeCreateResult:
        requested["branch"] = branch
        return WorktreeCreateResult(path=target_path, branch=branch, created=True)

    monkeypatch.setattr(lifecycle, "resolve_main_repo_root", fake_resolve_main_repo_root)
    monkeypatch.setattr(lifecycle, "create_worktree", fake_create_worktree)

    item = WorkItem(
        name="renamed-item",
        status="open",
        created_at="2026-05-09T00:00:00Z",
        worktree=WorktreeMetadata(
            path=str(missing_path),
            branch="feature/original-name",
            pending=False,
        ),
    )

    result = lifecycle.restore_for_reopen(tmp_path, item)

    assert result.status == "restored"
    assert requested["branch"] == "feature/original-name"
    assert result.metadata.branch == "feature/original-name"


def test_provision_for_start_preserves_existing_branch(monkeypatch, tmp_path: Path) -> None:
    requested: dict[str, str] = {}

    def fake_detect_git_repo(project_root: Path) -> bool:
        return True

    def fake_resolve_main_repo_root(project_root: Path) -> Path:
        return project_root

    def fake_create_worktree(
        repo_root: Path, target_path: Path, branch: str
    ) -> WorktreeCreateResult:
        requested["branch"] = branch
        return WorktreeCreateResult(path=target_path, branch=branch, created=True)

    monkeypatch.setattr(lifecycle, "detect_git_repo", fake_detect_git_repo)
    monkeypatch.setattr(lifecycle, "resolve_main_repo_root", fake_resolve_main_repo_root)
    monkeypatch.setattr(lifecycle, "create_worktree", fake_create_worktree)

    result = lifecycle.provision_for_start(
        tmp_path,
        "renamed-item",
        WorkConfig(default_worktree=True, worktree_base=None),
        existing=WorktreeMetadata(branch="feature/original-name"),
    )

    assert result.status == "provisioned"
    assert requested["branch"] == "feature/original-name"
    assert result.metadata.branch == "feature/original-name"
