from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.lib.hooks.builtin.git_autosync import GitAutosync
from meridian.plugin_api import Hook, HookContext
from tests.support.git import isolated_git_env

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI is required")


def _git(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        pytest.fail(
            "git command failed: "
            f"{' '.join(args)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _context(work_dir: Path) -> HookContext:
    return HookContext(
        event_name="work.done",
        event_id=uuid4(),
        timestamp="2026-04-20T00:00:00+00:00",
        project_root=str(work_dir),
        runtime_root=str(work_dir / ".meridian"),
        work_id="w123",
        work_dir=str(work_dir),
    )


def _hook(
    *,
    remote: str,
    exclude: tuple[str, ...] = (),
    options: dict[str, object] | None = None,
) -> Hook:
    return Hook(
        name="git-autosync",
        event="work.done",
        source="project",
        builtin="git-autosync",
        remote=remote,
        exclude=exclude,
        options=options or {},
    )
def _init_commit_repo(path: Path, *, env: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=path, env=env)
    repo_root = _git("rev-parse", "--show-toplevel", cwd=path, env=env).stdout.strip()
    assert Path(repo_root).resolve() == path.resolve()
    _git("config", "user.email", "autosync-test@example.com", cwd=path, env=env)
    _git("config", "user.name", "Autosync Test", cwd=path, env=env)
    (path / "shared.txt").write_text("seed\n", encoding="utf-8")
    (path / "keep.txt").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=path, env=env)
    _git("commit", "-m", "seed", cwd=path, env=env)


def _seed_remote(tmp_path: Path, *, env: dict[str, str]) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote), env=env)

    seed = tmp_path / "seed"
    _init_commit_repo(seed, env=env)
    _git("remote", "add", "origin", str(remote), cwd=seed, env=env)
    _git("push", "-u", "origin", "HEAD", cwd=seed, env=env)

    work = tmp_path / "work"
    _git("clone", str(remote), str(work), env=env)
    _git("config", "user.email", "autosync-test@example.com", cwd=work, env=env)
    _git("config", "user.name", "Autosync Test", cwd=work, env=env)
    return remote, work


def _current_branch(repo: Path, *, env: dict[str, str]) -> str:
    return _git("branch", "--show-current", cwd=repo, env=env).stdout.strip()


def _remote_head(remote: Path, branch: str, *, env: dict[str, str]) -> str:
    return _git(
        "--git-dir",
        str(remote),
        "rev-parse",
        f"refs/heads/{branch}",
        env=env,
    ).stdout.strip()


def _toml_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _configure_clone_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo_url: str,
    clone_path: Path,
) -> None:
    meridian_home = tmp_path / "meridian-home"
    meridian_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MERIDIAN_HOME", str(meridian_home))
    (meridian_home / "config.toml").write_text(
        "[git."
        f"\"{_toml_quote(repo_url)}\""
        "]\n"
        f'path = "{_toml_quote(str(clone_path))}"\n',
        encoding="utf-8",
    )


def test_git_autosync_syncs_and_pushes_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)
    branch = _current_branch(work, env=git_env)
    before_remote = _remote_head(remote, branch, env=git_env)

    (work / "keep.txt").write_text("local change\n", encoding="utf-8")
    (work / "new.txt").write_text("new file\n", encoding="utf-8")

    hook = GitAutosync()
    result = hook.execute(_context(work), _hook(remote=str(remote)))

    assert result.outcome == "success"
    assert result.success is True
    assert result.skipped is False

    subject = _git("log", "-1", "--pretty=%s", cwd=work, env=git_env).stdout.strip()
    assert subject.startswith("autosync: ")

    after_remote = _remote_head(remote, branch, env=git_env)
    assert before_remote != after_remote

    status = _git("status", "--porcelain", cwd=work, env=git_env).stdout
    assert status.strip() == ""


def test_git_autosync_first_time_clone_does_not_fail_when_lock_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote), env=git_env)

    seed = tmp_path / "seed"
    _init_commit_repo(seed, env=git_env)
    _git("remote", "add", "origin", str(remote), cwd=seed, env=git_env)
    _git("push", "-u", "origin", "HEAD", cwd=seed, env=git_env)

    clone_path = tmp_path / "fresh-clone"
    assert not clone_path.exists()
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=clone_path)

    hook = GitAutosync()
    result = hook.execute(_context(tmp_path), _hook(remote=str(remote)))

    assert result.success is True
    assert result.skip_reason == "nothing_to_sync"
    assert result.skip_reason != "clone_failed"
    assert (clone_path / ".git").exists()
    origin = _git("remote", "get-url", "origin", cwd=clone_path, env=git_env).stdout.strip()
    assert origin == str(remote)


def test_git_autosync_skips_when_user_state_lock_cannot_be_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)

    def _raise_permission(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("sandbox denied")

    monkeypatch.setattr("meridian.lib.hooks.builtin.git_autosync.file_lock", _raise_permission)

    result = GitAutosync().execute(_context(work), _hook(remote=str(remote)))

    assert result.outcome == "skipped"
    assert result.success is True
    assert result.skipped is True
    assert result.skip_reason == "lock_permission_error"


def test_git_autosync_excludes_configured_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)
    (work / "keep.txt").write_text("include me\n", encoding="utf-8")
    (work / "debug.log").write_text("exclude me\n", encoding="utf-8")
    (work / "tmp").mkdir()
    (work / "tmp" / "cache.txt").write_text("exclude dir\n", encoding="utf-8")

    hook = GitAutosync()
    result = hook.execute(
        _context(work),
        _hook(remote=str(remote), exclude=("*.log", "tmp/")),
    )

    assert result.outcome == "success"
    assert result.success is True

    changed = _git("show", "--pretty=format:", "--name-only", "HEAD", cwd=work, env=git_env).stdout
    changed_paths = {line.strip() for line in changed.splitlines() if line.strip()}
    assert "keep.txt" in changed_paths
    assert "debug.log" not in changed_paths
    assert "tmp/cache.txt" not in changed_paths

    status_lines = _git("status", "--porcelain", cwd=work, env=git_env).stdout.splitlines()
    assert any("debug.log" in line for line in status_lines)
    assert any("tmp/" in line or "tmp/cache.txt" in line for line in status_lines)


def test_git_autosync_leaves_rebase_conflict_for_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)
    branch = _current_branch(work, env=git_env)

    other = tmp_path / "other"
    _git("clone", str(remote), str(other), env=git_env)
    _git("config", "user.email", "autosync-test@example.com", cwd=other, env=git_env)
    _git("config", "user.name", "Autosync Test", cwd=other, env=git_env)

    (other / "shared.txt").write_text("remote change\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote change", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)
    remote_head_after_other = _remote_head(remote, branch, env=git_env)

    (work / "shared.txt").write_text("local change\n", encoding="utf-8")

    hook = GitAutosync()
    result = hook.execute(_context(work), _hook(remote=str(remote)))

    assert result.outcome == "skipped"
    assert result.success is True
    assert result.skipped is True
    assert result.skip_reason == "rebase_conflict"
    assert result.error is not None
    assert f"Rebase conflict at {work}" in result.error
    assert "Conflicts left for review" in result.error

    assert (work / ".git" / "rebase-merge").exists()
    assert not (work / ".git" / "rebase-apply").exists()
    conflicted_file = (work / "shared.txt").read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in conflicted_file
    assert "=======" in conflicted_file
    assert ">>>>>>> " in conflicted_file

    remote_head_after_hook = _remote_head(remote, branch, env=git_env)
    assert remote_head_after_hook == remote_head_after_other

    second_result = hook.execute(_context(work), _hook(remote=str(remote)))
    assert second_result.outcome == "skipped"
    assert second_result.success is True
    assert second_result.skipped is True
    assert second_result.skip_reason == "existing_rebase_conflict"
