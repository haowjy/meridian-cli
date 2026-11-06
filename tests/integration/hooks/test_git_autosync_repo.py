from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from meridian.lib.hooks.builtin import autosync_store
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
        runtime_root=str(work_dir.parent / "runtime"),
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
        f'[git."{_toml_quote(repo_url)}"]\npath = "{_toml_quote(str(clone_path))}"\n',
        encoding="utf-8",
    )


def _read_conflicts(work: Path) -> list[dict[str, object]]:
    """Read all conflict metadata files from a sync root."""

    return [
        cast("dict[str, object]", record.model_dump())
        for record in autosync_store.read_conflicts(work, runtime_root=work.parent / "runtime")
    ]


def _git_path(repo: Path, name: str, *, env: dict[str, str]) -> Path:
    path_str = _git("rev-parse", "--git-path", name, cwd=repo, env=env).stdout.strip()
    path = Path(path_str)
    if not path.is_absolute():
        path = repo / path
    return path


def _clone_remote(remote: Path, target: Path, *, env: dict[str, str]) -> Path:
    _git("clone", str(remote), str(target), env=env)
    _git("config", "user.email", "autosync-test@example.com", cwd=target, env=env)
    _git("config", "user.name", "Autosync Test", cwd=target, env=env)
    return target


def test_git_autosync_ignores_hostile_repo_scoped_git_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)

    decoy = tmp_path / "decoy"
    _init_commit_repo(decoy, env=git_env)
    intended_before = _git("rev-parse", "HEAD", cwd=work, env=git_env).stdout.strip()
    decoy_before = _git("rev-parse", "HEAD", cwd=decoy, env=git_env).stdout.strip()
    (work / "keep.txt").write_text("intended repository change\n", encoding="utf-8")

    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

    result = GitAutosync().execute(
        _context(work),
        _hook(remote="", options={"path": str(work)}),
    )

    assert result.outcome == "success"
    intended_after = _git("rev-parse", "HEAD", cwd=work, env=git_env).stdout.strip()
    decoy_after = _git("rev-parse", "HEAD", cwd=decoy, env=git_env).stdout.strip()
    assert intended_after != intended_before
    assert decoy_after == decoy_before
    assert (
        _git("show", "HEAD:keep.txt", cwd=work, env=git_env).stdout
        == "intended repository change\n"
    )
    assert _git("status", "--porcelain", cwd=decoy, env=git_env).stdout == ""


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


def test_git_autosync_merge_conflict_aborts_and_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)
    branch = _current_branch(work, env=git_env)

    other = _clone_remote(remote, tmp_path / "other", env=git_env)
    (other / "shared.txt").write_text("remote change\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote change", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)
    remote_head_after_other = _remote_head(remote, branch, env=git_env)

    (work / "shared.txt").write_text("local change\n", encoding="utf-8")

    result = GitAutosync().execute(_context(work), _hook(remote=str(remote)))

    assert result.outcome == "skipped"
    assert result.success is True
    assert result.skipped is True
    assert result.skip_reason == "conflict_detected"

    assert not _git_path(work, "rebase-merge", env=git_env).exists()
    assert not _git_path(work, "rebase-apply", env=git_env).exists()
    assert not _git_path(work, "MERGE_HEAD", env=git_env).exists()

    merged_file = (work / "shared.txt").read_text(encoding="utf-8")
    assert merged_file == "local change\n"
    assert "<<<<<<<" not in merged_file

    conflicts = _read_conflicts(work)
    assert len(conflicts) == 1
    assert conflicts[0].get("id")
    assert conflicts[0].get("resolved") is False

    remote_head_after_hook = _remote_head(remote, branch, env=git_env)
    assert remote_head_after_hook == remote_head_after_other


def test_git_autosync_clean_merge_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)
    branch = _current_branch(work, env=git_env)

    other = _clone_remote(remote, tmp_path / "other", env=git_env)
    (other / "file_a.txt").write_text("from remote\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote adds file_a", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)

    (work / "file_b.txt").write_text("from local\n", encoding="utf-8")

    result = GitAutosync().execute(_context(work), _hook(remote=str(remote)))

    assert result.outcome == "success"
    assert result.success is True
    assert (work / "file_a.txt").read_text(encoding="utf-8") == "from remote\n"
    assert (work / "file_b.txt").read_text(encoding="utf-8") == "from local\n"

    local_head = _git("rev-parse", "HEAD", cwd=work, env=git_env).stdout.strip()
    remote_head = _remote_head(remote, branch, env=git_env)
    assert local_head == remote_head


def test_git_autosync_retry_after_conflict_resolves_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)
    branch = _current_branch(work, env=git_env)

    other = _clone_remote(remote, tmp_path / "other", env=git_env)
    (other / "shared.txt").write_text("remote change\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote change", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)

    (work / "shared.txt").write_text("local change\n", encoding="utf-8")

    hook = GitAutosync()
    first = hook.execute(_context(work), _hook(remote=str(remote)))

    assert first.outcome == "skipped"
    assert first.skip_reason == "conflict_detected"
    assert _read_conflicts(work)

    (other / "shared.txt").write_text("local change\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote aligns", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)

    second = hook.execute(_context(work), _hook(remote=str(remote)))

    assert second.outcome == "success"
    assert second.success is True

    local_head = _git("rev-parse", "HEAD", cwd=work, env=git_env).stdout.strip()
    remote_head = _remote_head(remote, branch, env=git_env)
    assert local_head == remote_head


def test_git_autosync_aborts_stale_rebase_on_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)
    branch = _current_branch(work, env=git_env)

    other = _clone_remote(remote, tmp_path / "other", env=git_env)
    (other / "shared.txt").write_text("remote change\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote change", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)

    (work / "shared.txt").write_text("local change\n", encoding="utf-8")
    _git("add", "-A", cwd=work, env=git_env)
    _git("commit", "-m", "local change", cwd=work, env=git_env)

    rebase = _git("pull", "--rebase", "origin", branch, cwd=work, env=git_env, check=False)
    assert rebase.returncode != 0
    assert (
        _git_path(work, "rebase-merge", env=git_env).exists()
        or _git_path(work, "rebase-apply", env=git_env).exists()
    )

    (other / "shared.txt").write_text("local change\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote aligns", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)

    result = GitAutosync().execute(_context(work), _hook(remote=str(remote)))

    assert result.success is True
    assert not _git_path(work, "rebase-merge", env=git_env).exists()
    assert not _git_path(work, "rebase-apply", env=git_env).exists()


