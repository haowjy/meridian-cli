from __future__ import annotations

import shutil
import subprocess
import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from meridian.lib.hooks.builtin import autosync_store
from meridian.lib.hooks.builtin.autosync_store import AutosyncMutation
from meridian.lib.hooks.builtin.git_autosync import GitAutosync
from meridian.lib.ops import sync_conflicts
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
        cast("dict[str, object]", asdict(record))
        for record in autosync_store.read_conflicts(
            work, runtime_root=work.parent / "runtime"
        )
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

    monkeypatch.setattr("meridian.lib.hooks.builtin.autosync_store.lock_file", _raise_permission)

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


def test_git_autosync_merge_conflict_does_not_rewrite_agents_md(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)

    other = _clone_remote(remote, tmp_path / "other", env=git_env)
    (other / "shared.txt").write_text("remote change\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote change", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)

    (work / "AGENTS.md").write_text("# Test\n", encoding="utf-8")
    (work / "shared.txt").write_text("local change\n", encoding="utf-8")

    result = GitAutosync().execute(_context(work), _hook(remote=str(remote)))

    assert result.outcome == "skipped"
    assert result.skip_reason == "conflict_detected"

    agents_md = (work / "AGENTS.md").read_text(encoding="utf-8")
    assert agents_md == "# Test\n"

    subjects = _git("log", "--pretty=%s", cwd=work, env=git_env).stdout.splitlines()
    assert not any(subject.startswith("autosync: conflict notice") for subject in subjects)


def test_conflict_resolution_waits_for_complete_hook_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)

    other = _clone_remote(remote, tmp_path / "other", env=git_env)
    (other / "shared.txt").write_text("remote change\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote change", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)
    (work / "shared.txt").write_text("local change\n", encoding="utf-8")

    record_written = threading.Event()
    release_hook = threading.Event()
    resolution_started = threading.Event()
    original_write_conflict = autosync_store._write_conflict
    original_transaction = autosync_store.transaction

    def delayed_write_conflict(
        sync_root: Path,
        record: autosync_store.ConflictRecord,
        *,
        runtime_root: Path,
    ) -> None:
        original_write_conflict(sync_root, record, runtime_root=runtime_root)
        record_written.set()
        assert release_hook.wait(timeout=5)

    @contextmanager
    def observed_resolution_transaction(
        sync_root: Path,
        *,
        runtime_root: Path,
        timeout: float | None = 60.0,
    ) -> Generator[AutosyncMutation, None, None]:
        resolution_started.set()
        with original_transaction(
            sync_root, runtime_root=runtime_root, timeout=timeout
        ) as autosync_tx:
            yield autosync_tx

    monkeypatch.setattr(autosync_store, "_write_conflict", delayed_write_conflict)
    monkeypatch.setattr(
        sync_conflicts,
        "_find_sync_roots",
        lambda: ([work], work.parent / "runtime"),
    )
    monkeypatch.setattr(sync_conflicts, "transaction", observed_resolution_transaction)

    with ThreadPoolExecutor(max_workers=2) as executor:
        hook = executor.submit(GitAutosync().execute, _context(work), _hook(remote=str(remote)))
        assert record_written.wait(timeout=5)
        [record] = _read_conflicts(work)
        conflict_id = cast("str", record["id"])
        resolve = executor.submit(sync_conflicts.resolve_conflict_sync, conflict_id)
        assert resolution_started.wait(timeout=5)
        release_hook.set()
        hook_result = hook.result()
        resolve_result = resolve.result()

    assert hook_result.skip_reason == "conflict_detected"
    assert resolve_result.resolved is True
    [stored] = _read_conflicts(work)
    assert stored["resolved"] is True


def test_git_autosync_merge_conflict_skips_notice_when_no_agents_md(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)

    other = _clone_remote(remote, tmp_path / "other", env=git_env)
    (other / "shared.txt").write_text("remote change\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote change", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)

    assert not (work / "AGENTS.md").exists()
    (work / "shared.txt").write_text("local change\n", encoding="utf-8")

    result = GitAutosync().execute(_context(work), _hook(remote=str(remote)))

    assert result.outcome == "skipped"
    assert result.skip_reason == "conflict_detected"
    assert not (work / "AGENTS.md").exists()

    conflicts = _read_conflicts(work)
    assert len(conflicts) == 1


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


def test_git_autosync_delete_remote_merges_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)

    other = _clone_remote(remote, tmp_path / "other", env=git_env)
    (other / "keep.txt").unlink()
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote deletes keep", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)

    result = GitAutosync().execute(_context(work), _hook(remote=str(remote)))

    assert result.outcome == "success"
    assert result.success is True
    assert not (work / "keep.txt").exists()


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


def test_git_autosync_default_ignores_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)

    result = GitAutosync().execute(_context(work), _hook(remote=str(remote)))

    assert result.success is True

    exclude_contents = (work / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".git" in exclude_contents
    assert "**/.git" in exclude_contents
    assert ".meridian/autosync/" not in exclude_contents


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
    assert _git_path(work, "rebase-merge", env=git_env).exists() or _git_path(
        work, "rebase-apply", env=git_env
    ).exists()

    (other / "shared.txt").write_text("local change\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote aligns", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)

    result = GitAutosync().execute(_context(work), _hook(remote=str(remote)))

    assert result.success is True
    assert not _git_path(work, "rebase-merge", env=git_env).exists()
    assert not _git_path(work, "rebase-apply", env=git_env).exists()


def test_git_autosync_conflict_metadata_not_staged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    remote, work = _seed_remote(tmp_path, env=git_env)
    _configure_clone_override(tmp_path, monkeypatch, repo_url=str(remote), clone_path=work)

    other = _clone_remote(remote, tmp_path / "other", env=git_env)
    (other / "shared.txt").write_text("remote change\n", encoding="utf-8")
    _git("add", "-A", cwd=other, env=git_env)
    _git("commit", "-m", "remote change", cwd=other, env=git_env)
    _git("push", "origin", "HEAD", cwd=other, env=git_env)

    (work / "shared.txt").write_text("local change\n", encoding="utf-8")

    result = GitAutosync().execute(_context(work), _hook(remote=str(remote)))

    assert result.outcome == "skipped"
    assert result.skip_reason == "conflict_detected"
    assert _read_conflicts(work)

    touched = _git("log", "--all", "--name-only", "--pretty=format:", cwd=work, env=git_env).stdout
    touched_paths = [line.strip() for line in touched.splitlines() if line.strip()]
    assert not any(path.startswith(".meridian/autosync/") for path in touched_paths)
