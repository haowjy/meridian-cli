from pathlib import Path

import pytest

from meridian.lib.harness.claude import project_slug
from meridian.lib.harness.claude_preflight import ensure_claude_session_accessible
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
