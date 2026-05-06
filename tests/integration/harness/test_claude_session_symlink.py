import os
from pathlib import Path

import pytest

import meridian.lib.harness.claude_preflight as claude_preflight
from meridian.lib.harness.claude import project_slug
from meridian.lib.harness.claude_preflight import (
    ensure_claude_session_accessible,
    materialize_overlay_transcripts,
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
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

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
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

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
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

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
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

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
        assert not target_file.is_symlink()
        assert target_file.read_text(encoding="utf-8") == source_file.read_text(encoding="utf-8")


def test_ensure_claude_session_accessible_falls_back_to_canonical_when_source_root_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    canonical_root = tmp_path / "canonical"
    source_config_root = tmp_path / "deleted-overlay"
    target_config_root = tmp_path / "target-overlay"
    source_cwd = tmp_path / "source"
    child_cwd = tmp_path / "child"
    source_cwd.mkdir()
    child_cwd.mkdir()
    fake_home.mkdir()
    canonical_root.mkdir()
    monkeypatch.setenv("HOME", fake_home.as_posix())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", canonical_root.as_posix())

    source_project = canonical_root / "projects" / project_slug(source_cwd)
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
    assert target_file.read_text(encoding="utf-8") == source_file.read_text(encoding="utf-8")
    assert not target_file.is_symlink()


def test_ensure_claude_session_accessible_seeds_same_cwd_when_config_roots_differ(
    tmp_path: Path,
) -> None:
    source_config_root = tmp_path / "source-overlay"
    target_config_root = tmp_path / "target-overlay"
    source_cwd = tmp_path / "project"
    source_cwd.mkdir()

    source_project = source_config_root / "projects" / project_slug(source_cwd)
    source_project.mkdir(parents=True)
    source_file = source_project / "session-1.jsonl"
    source_file.write_text('{"sessionId":"session-1"}\n', encoding="utf-8")

    ensure_claude_session_accessible(
        "session-1",
        source_cwd,
        source_cwd,
        source_config_root=source_config_root,
        target_config_root=target_config_root,
    )

    target_file = target_config_root / "projects" / project_slug(source_cwd) / "session-1.jsonl"
    assert target_file.exists()
    assert target_file.read_text(encoding="utf-8") == source_file.read_text(encoding="utf-8")
    assert not target_file.is_symlink()


def test_ensure_claude_session_accessible_defaults_target_root_to_claude_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    canonical_root = tmp_path / "custom-claude"
    source_cwd = tmp_path / "source"
    child_cwd = tmp_path / "child"
    source_cwd.mkdir()
    child_cwd.mkdir()
    fake_home.mkdir()
    canonical_root.mkdir()
    monkeypatch.setenv("HOME", fake_home.as_posix())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", canonical_root.as_posix())

    source_project = canonical_root / "projects" / project_slug(source_cwd)
    source_project.mkdir(parents=True)
    (source_project / "session-1.jsonl").write_text('{"sessionId":"session-1"}\n', encoding="utf-8")

    ensure_claude_session_accessible("session-1", source_cwd, child_cwd)

    target_file = canonical_root / "projects" / project_slug(child_cwd) / "session-1.jsonl"
    default_target = (
        fake_home / ".claude" / "projects" / project_slug(child_cwd) / "session-1.jsonl"
    )
    assert target_file.exists()
    assert not default_target.exists()


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
    fake_home = tmp_path / "home"
    user_root = fake_home / ".claude"
    user_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", fake_home.as_posix())
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    credentials = fake_home / ".claude.json"
    credentials.write_text('{"auth":true}', encoding="utf-8")

    isolated_root, _ = prepare_isolated_claude_config(tmp_path / "runtime", "p1")

    assert isolated_root is not None
    isolated_credentials = isolated_root / ".claude.json"
    assert isolated_credentials.exists()
    assert isolated_credentials.read_text(encoding="utf-8") == '{"auth":true}'
    assert not isolated_credentials.is_symlink()


def test_prepare_isolated_claude_config_prefers_config_root_credentials_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    user_root = tmp_path / "user-claude"
    user_root.mkdir()
    fake_home.mkdir()
    monkeypatch.setenv("HOME", fake_home.as_posix())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", user_root.as_posix())
    (fake_home / ".claude.json").write_text('{"auth":"home"}', encoding="utf-8")
    (user_root / ".claude.json").write_text('{"auth":"config"}', encoding="utf-8")

    isolated_root, original_env = prepare_isolated_claude_config(tmp_path / "runtime", "p1")

    assert isolated_root is not None
    assert original_env == user_root.as_posix()
    assert (isolated_root / ".claude.json").read_text(encoding="utf-8") == '{"auth":"config"}'


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


def test_prepare_isolated_claude_config_prefers_internal_original_root_over_parent_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_overlay = tmp_path / "parent-overlay"
    durable_root = tmp_path / "durable-root"
    parent_overlay.mkdir()
    durable_root.mkdir()
    (parent_overlay / "settings.json").write_text('{"source":"overlay"}', encoding="utf-8")
    (durable_root / "settings.json").write_text('{"source":"durable"}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", parent_overlay.as_posix())
    monkeypatch.setenv(
        claude_preflight.MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV,
        durable_root.as_posix(),
    )

    isolated_root, original_env = prepare_isolated_claude_config(tmp_path / "runtime", "p1")

    assert isolated_root is not None
    assert original_env == durable_root.as_posix()
    assert (isolated_root / "settings.json").read_text(encoding="utf-8") == '{"source":"durable"}'


def test_prepare_isolated_claude_config_uses_default_root_when_original_metadata_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    parent_overlay = tmp_path / "parent-overlay"
    default_root = fake_home / ".claude"
    fake_home.mkdir()
    parent_overlay.mkdir()
    default_root.mkdir(parents=True)
    (parent_overlay / "settings.json").write_text('{"source":"overlay"}', encoding="utf-8")
    (default_root / "settings.json").write_text('{"source":"default"}', encoding="utf-8")
    monkeypatch.setenv("HOME", fake_home.as_posix())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", parent_overlay.as_posix())
    monkeypatch.setenv(claude_preflight.MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV, "")

    isolated_root, original_env = prepare_isolated_claude_config(tmp_path / "runtime", "p1")

    assert isolated_root is not None
    assert original_env == ""
    assert (isolated_root / "settings.json").read_text(encoding="utf-8") == '{"source":"default"}'


def test_materialize_overlay_transcripts_copies_all_jsonl_files(
    tmp_path: Path,
) -> None:
    overlay_root = tmp_path / "overlay"
    canonical_root = tmp_path / "canonical"
    overlay_project = overlay_root / "projects" / "slug-a"
    overlay_project.mkdir(parents=True)
    first = overlay_project / "session-1.jsonl"
    second = overlay_project / "session-2.jsonl"
    ignored = overlay_project / "notes.txt"
    first.write_text('{"sessionId":"session-1"}\n', encoding="utf-8")
    second.write_text('{"sessionId":"session-2"}\n', encoding="utf-8")
    ignored.write_text("ignore me\n", encoding="utf-8")

    copied = materialize_overlay_transcripts(overlay_root, canonical_root=canonical_root)

    assert copied == 2
    assert (canonical_root / "projects" / "slug-a" / "session-1.jsonl").read_text(
        encoding="utf-8"
    ) == first.read_text(encoding="utf-8")
    assert (canonical_root / "projects" / "slug-a" / "session-2.jsonl").read_text(
        encoding="utf-8"
    ) == second.read_text(encoding="utf-8")
    assert not (canonical_root / "projects" / "slug-a" / "notes.txt").exists()


def test_materialize_overlay_transcripts_prefers_newer_or_larger_overlay_copy(
    tmp_path: Path,
) -> None:
    overlay_root = tmp_path / "overlay"
    canonical_root = tmp_path / "canonical"
    overlay_project = overlay_root / "projects" / "slug-a"
    canonical_project = canonical_root / "projects" / "slug-a"
    overlay_project.mkdir(parents=True)
    canonical_project.mkdir(parents=True)

    target = canonical_project / "session.jsonl"
    source = overlay_project / "session.jsonl"

    target.write_text("old\n", encoding="utf-8")
    source.write_text("newer payload\n", encoding="utf-8")

    stale_mtime = 1_700_000_000
    newer_mtime = stale_mtime + 10
    os.utime(target, (stale_mtime, stale_mtime))
    os.utime(source, (newer_mtime, newer_mtime))

    copied = materialize_overlay_transcripts(overlay_root, canonical_root=canonical_root)
    assert copied == 1
    assert target.read_text(encoding="utf-8") == "newer payload\n"

    target.write_text("tiny\n", encoding="utf-8")
    source.write_text("bigger replacement\n", encoding="utf-8")
    tied_mtime = newer_mtime + 10
    os.utime(target, (tied_mtime, tied_mtime))
    os.utime(source, (tied_mtime, tied_mtime))

    copied = materialize_overlay_transcripts(overlay_root, canonical_root=canonical_root)
    assert copied == 1
    assert target.read_text(encoding="utf-8") == "bigger replacement\n"


def test_materialize_overlay_transcripts_uses_atomic_replace_and_cleans_temp_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay_root = tmp_path / "overlay"
    canonical_root = tmp_path / "canonical"
    overlay_project = overlay_root / "projects" / "slug-a"
    canonical_project = canonical_root / "projects" / "slug-a"
    overlay_project.mkdir(parents=True)
    canonical_project.mkdir(parents=True)

    target = canonical_project / "session.jsonl"
    source = overlay_project / "session.jsonl"
    target.write_text("old\n", encoding="utf-8")
    source.write_text("new\n", encoding="utf-8")
    os.utime(target, (1_700_000_000, 1_700_000_000))
    os.utime(source, (1_700_000_010, 1_700_000_010))

    attempted_temp_paths: list[Path] = []

    def _raise_replace(src: str, dst: str) -> None:
        attempted_temp_paths.append(Path(src))
        raise OSError("replace failed")

    monkeypatch.setattr(claude_preflight.os, "replace", _raise_replace)

    copied = materialize_overlay_transcripts(overlay_root, canonical_root=canonical_root)

    assert copied == 0
    assert target.read_text(encoding="utf-8") == "old\n"
    assert attempted_temp_paths
    assert all(not temp_path.exists() for temp_path in attempted_temp_paths)
    assert list(canonical_project.glob(".session.jsonl.*.tmp")) == []


def test_materialize_overlay_transcripts_is_noop_for_missing_projects(tmp_path: Path) -> None:
    assert (
        materialize_overlay_transcripts(tmp_path / "overlay", canonical_root=tmp_path / "out")
        == 0
    )
