from pathlib import Path

import pytest

from meridian.lib.harness.claude import project_slug
from meridian.lib.harness.claude_preflight import (
    ensure_claude_session_accessible,
    prepare_isolated_claude_config,
)
from meridian.lib.platform import IS_WINDOWS


def _write_session_file(home: Path, project_root: Path, session_id: str) -> Path:
    project_dir = home / ".claude" / "projects" / project_slug(project_root)
    project_dir.mkdir(parents=True, exist_ok=True)
    session_file = project_dir / f"{session_id}.jsonl"
    session_file.write_text(f'{{"sessionId":"{session_id}"}}\n', encoding="utf-8")
    return session_file


def _target_session_file(home: Path, project_root: Path, session_id: str) -> Path:
    return home / ".claude" / "projects" / project_slug(project_root) / f"{session_id}.jsonl"


def test_ensure_claude_session_accessible_is_noop_when_source_cwd_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    child_cwd = tmp_path / "child"
    child_cwd.mkdir()

    ensure_claude_session_accessible("session-1", None, child_cwd)

    assert not (fake_home / ".claude").exists()


def test_ensure_claude_session_accessible_makes_session_available_in_child_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    source_cwd = tmp_path / "source"
    child_cwd = tmp_path / "child"
    source_cwd.mkdir()
    child_cwd.mkdir()

    source_file = _write_session_file(fake_home, source_cwd, "session-1")

    ensure_claude_session_accessible("session-1", source_cwd, child_cwd)

    target_file = _target_session_file(fake_home, child_cwd, "session-1")
    assert target_file.exists()
    # On POSIX: symlink. On Windows: copy.
    if IS_WINDOWS:
        assert target_file.read_text() == source_file.read_text()
    else:
        assert target_file.is_symlink()
        assert target_file.resolve() == source_file.resolve()


def test_ensure_claude_session_accessible_is_idempotent_on_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    source_cwd = tmp_path / "source"
    child_cwd = tmp_path / "child"
    source_cwd.mkdir()
    child_cwd.mkdir()

    source_file = _write_session_file(fake_home, source_cwd, "session-1")
    target_file = _target_session_file(fake_home, child_cwd, "session-1")
    target_file.parent.mkdir(parents=True, exist_ok=True)

    if IS_WINDOWS:
        # Pre-create as copy
        target_file.write_text(source_file.read_text())
    else:
        # Pre-create as symlink
        target_file.symlink_to(source_file)

    # Should tolerate existing file/symlink
    ensure_claude_session_accessible("session-1", source_cwd, child_cwd)

    assert target_file.exists()
    if IS_WINDOWS:
        assert target_file.read_text() == source_file.read_text()
    else:
        assert target_file.is_symlink()
        assert target_file.resolve() == source_file.resolve()


@pytest.mark.parametrize("session_id", ("../../evil", "foo/bar"))
def test_ensure_claude_session_accessible_rejects_path_traversal_session_ids(
    session_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    source_cwd = tmp_path / "source"
    child_cwd = tmp_path / "child"
    source_cwd.mkdir()
    child_cwd.mkdir()
    _write_session_file(fake_home, source_cwd, "safe-session")

    ensure_claude_session_accessible(session_id, source_cwd, child_cwd)

    child_project = fake_home / ".claude" / "projects" / project_slug(child_cwd)
    assert not child_project.exists()


def test_ensure_claude_session_accessible_uses_explicit_source_and_target_roots(
    tmp_path: Path,
) -> None:
    source_config_root = tmp_path / "source-config"
    target_config_root = tmp_path / "target-config"
    source_cwd = tmp_path / "source"
    child_cwd = tmp_path / "child"
    source_cwd.mkdir()
    child_cwd.mkdir()

    source_project = source_config_root / "projects" / project_slug(source_cwd)
    source_project.mkdir(parents=True)
    source_file = source_project / "session-1.jsonl"
    source_file.write_text('{"sessionId":"session-1"}\n', encoding="utf-8")

    ensure_claude_session_accessible(
        "session-1",
        source_cwd,
        child_cwd,
        source_config_root=source_config_root,
        target_config_root=target_config_root,
    )

    target_file = target_config_root / "projects" / project_slug(child_cwd) / "session-1.jsonl"
    assert target_file.exists()
    if IS_WINDOWS:
        assert target_file.read_text(encoding="utf-8") == source_file.read_text(encoding="utf-8")
    else:
        assert target_file.is_symlink()
        assert target_file.resolve() == source_file.resolve()


def test_prepare_isolated_claude_config_creates_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_root = tmp_path / "user-claude"
    user_root.mkdir()
    immutable_file = user_root / "settings.json"
    immutable_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", user_root.as_posix())

    isolated_root, original_env = prepare_isolated_claude_config(tmp_path / "runtime", "p1")

    assert isolated_root == tmp_path / "runtime" / "claude-config" / "p1"
    assert original_env == user_root.as_posix()
    assert isolated_root is not None
    target = isolated_root / "settings.json"
    assert target.exists()
    if IS_WINDOWS:
        assert target.read_text(encoding="utf-8") == "{}"
    else:
        assert target.is_symlink()
        assert target.resolve() == immutable_file.resolve()


def test_prepare_isolated_claude_config_isolates_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_root = tmp_path / "user-claude"
    source_project = user_root / "projects" / "source"
    source_project.mkdir(parents=True)
    (source_project / "session.jsonl").write_text("source\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", user_root.as_posix())

    isolated_root, _ = prepare_isolated_claude_config(tmp_path / "runtime", "p1")

    assert isolated_root is not None
    assert (isolated_root / "projects").is_dir()
    assert list((isolated_root / "projects").iterdir()) == []


@pytest.mark.parametrize("name", ("statsig", "memory", "cached_preferences", "todos"))
def test_prepare_isolated_claude_config_copies_mutable_paths(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_root = tmp_path / "user-claude"
    mutable_dir = user_root / name
    mutable_dir.mkdir(parents=True)
    source_file = mutable_dir / "state.json"
    source_file.write_text("before", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", user_root.as_posix())

    isolated_root, _ = prepare_isolated_claude_config(tmp_path / "runtime", "p1")

    assert isolated_root is not None
    copied_file = isolated_root / name / "state.json"
    assert copied_file.read_text(encoding="utf-8") == "before"
    source_file.write_text("after", encoding="utf-8")
    assert copied_file.read_text(encoding="utf-8") == "before"


def test_prepare_isolated_claude_config_handles_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_root = tmp_path / "user-claude"
    user_root.mkdir()
    credentials = user_root / ".claude.json"
    credentials.write_text('{"auth":true}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", user_root.as_posix())

    isolated_root, _ = prepare_isolated_claude_config(tmp_path / "runtime", "p1")

    assert isolated_root is not None
    isolated_credentials = isolated_root / ".claude.json"
    assert isolated_credentials.exists()
    assert isolated_credentials.read_text(encoding="utf-8") == '{"auth":true}'
    if not IS_WINDOWS:
        assert isolated_credentials.is_symlink()
        assert isolated_credentials.resolve() == credentials.resolve()


def test_prepare_isolated_claude_config_failure_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_root = tmp_path / "user-claude"
    user_root.mkdir()
    runtime_root = tmp_path / "runtime-file"
    runtime_root.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", user_root.as_posix())

    isolated_root, original_env = prepare_isolated_claude_config(runtime_root, "p1")

    assert isolated_root is None
    assert original_env == user_root.as_posix()
