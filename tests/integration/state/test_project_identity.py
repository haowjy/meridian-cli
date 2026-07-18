from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from meridian.lib.config.project_root import cwd_has_project_id
from meridian.lib.ops.migration import migrate_project_id
from meridian.lib.state.user_paths import (
    get_or_create_project_id,
    get_project_id,
    write_project_id,
)


def test_project_predicate_recognizes_either_toml(tmp_path: Path) -> None:
    assert not cwd_has_project_id(tmp_path)
    (tmp_path / "mars.toml").write_text("[settings]\n", encoding="utf-8")
    assert cwd_has_project_id(tmp_path)
    (tmp_path / "mars.toml").unlink()
    (tmp_path / "meridian.toml").write_text("[defaults]\n", encoding="utf-8")
    assert cwd_has_project_id(tmp_path)


def test_committed_identity_is_read_without_minting(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text(
        '[project]\nid = "shared-clone-id"\n', encoding="utf-8"
    )
    assert get_project_id(tmp_path) == "shared-clone-id"
    assert get_or_create_project_id(tmp_path) == "shared-clone-id"
    assert not (tmp_path / ".meridian").exists()


def test_mars_only_write_creates_meridian_identity(tmp_path: Path) -> None:
    (tmp_path / "mars.toml").write_text("[settings]\n", encoding="utf-8")
    project_id = get_or_create_project_id(tmp_path)
    payload = tomllib.loads((tmp_path / "meridian.toml").read_text(encoding="utf-8"))
    assert payload["project"]["id"] == project_id
    assert not (tmp_path / ".meridian").exists()


def test_append_identity_preserves_existing_content_verbatim(tmp_path: Path) -> None:
    original = "# user formatting\n[defaults]\nmax_depth=7 # keep this\n"
    (tmp_path / "meridian.toml").write_text(original, encoding="utf-8")
    write_project_id(tmp_path, "preserved-project-id")
    updated = (tmp_path / "meridian.toml").read_text(encoding="utf-8")
    assert updated.startswith(original)
    assert updated.removeprefix(original) == (
        '\n# managed by meridian — do not edit\n[project]\nid = "preserved-project-id"\n'
    )


def test_failed_atomic_identity_write_leaves_existing_toml_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = "[defaults]\nmax_depth = 4\n"
    config_path = tmp_path / "meridian.toml"
    config_path.write_text(original, encoding="utf-8")

    def interrupted(_path: Path, _content: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("meridian.lib.config.preserving_edit.atomic_write_text", interrupted)
    with pytest.raises(KeyboardInterrupt):
        write_project_id(tmp_path, "never-committed")
    assert config_path.read_text(encoding="utf-8") == original
    assert tomllib.loads(original)["defaults"]["max_depth"] == 4
    assert not (tmp_path / ".meridian.toml.identity.lock").exists()


def test_migration_writes_identity_and_removes_legacy_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / ".meridian"
    legacy.mkdir()
    (legacy / "id").write_text("legacy-project-id", encoding="utf-8")
    (legacy / ".gitignore").write_text("*\n!id\n", encoding="utf-8")
    monkeypatch.setattr("meridian.lib.ops.migration._get_active_spawns", lambda _id: [])

    result = migrate_project_id(tmp_path)

    assert result.status == "migrated"
    assert get_project_id(tmp_path) == "legacy-project-id"
    assert not legacy.exists()


def test_migration_resumes_after_legacy_straggler_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / ".meridian"
    legacy.mkdir()
    (legacy / "id").write_text("legacy-project-id", encoding="utf-8")
    write_project_id(tmp_path, "legacy-project-id")
    monkeypatch.setattr("meridian.lib.ops.migration._get_active_spawns", lambda _id: [])

    result = migrate_project_id(tmp_path)

    assert result.status == "migrated"
    assert result.removed_legacy_identity
    assert not result.removed_legacy_gitignore
    assert get_project_id(tmp_path) == "legacy-project-id"
    assert not legacy.exists()


def test_migration_blocks_when_active_spawn_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / ".meridian"
    legacy.mkdir()
    (legacy / "id").write_text("legacy-project-id", encoding="utf-8")

    def fail_check(_project_id: str) -> list[str]:
        raise OSError("runtime state unreadable")

    monkeypatch.setattr("meridian.lib.ops.migration._get_active_spawns", fail_check)

    result = migrate_project_id(tmp_path)

    assert result.status == "blocked"
    assert result.blocking_reason == "Could not verify active spawns: runtime state unreadable"
    assert (legacy / "id").is_file()
    assert get_project_id(tmp_path) is None
