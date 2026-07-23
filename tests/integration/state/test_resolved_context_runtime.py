"""ResolvedContext runtime-root derivation from project identity."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.ops.runtime import resolve_runtime_authority_for_read
from meridian.lib.state.runtime_root import derive_runtime_root_from_project
from meridian.lib.state.user_paths import get_project_home

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_derive_runtime_root_uses_project_id_user_home(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    (project_root / "meridian.toml").write_text(
        '[project]\nid = "proj-abc"\n', encoding="utf-8"
    )

    derived = derive_runtime_root_from_project(project_root)

    assert derived == get_project_home("proj-abc")


def test_derive_runtime_root_is_unresolved_without_identity(tmp_path: Path) -> None:
    project_root = tmp_path / "plain"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "spawns.jsonl").write_text("", encoding="utf-8")

    derived = derive_runtime_root_from_project(project_root)

    assert derived is None


def test_resolve_runtime_authority_ignores_runtime_env_when_flagged(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    (project_root / "meridian.toml").write_text(
        '[project]\nid = "proj-flag"\n', encoding="utf-8"
    )
    override = tmp_path / "override"
    override.mkdir()
    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", override.as_posix())

    authority = resolve_runtime_authority_for_read(
        project_root,
        ignore_runtime_env=True,
    )

    assert authority.runtime_root == get_project_home("proj-flag")


def test_resolve_runtime_authority_derives_when_runtime_dir_absent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """When -C scope unsets _MERIDIAN_RUNTIME_DIR, runtime derives from project identity."""
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    (project_root / "meridian.toml").write_text(
        '[project]\nid = "proj-explicit"\n', encoding="utf-8"
    )
    monkeypatch.delenv("_MERIDIAN_DEPTH", raising=False)
    monkeypatch.delenv("_MERIDIAN_RUNTIME_DIR", raising=False)

    authority = resolve_runtime_authority_for_read(project_root)

    assert authority.runtime_root == get_project_home("proj-explicit")


def test_resolve_runtime_authority_honors_empty_override_over_project_state(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "spawns.jsonl").write_text("", encoding="utf-8")
    override = tmp_path / "empty-override"
    override.mkdir()
    monkeypatch.delenv("_MERIDIAN_DEPTH", raising=False)
    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", override.as_posix())

    authority = resolve_runtime_authority_for_read(project_root)

    assert authority.runtime_root == override.resolve()
    assert authority.runtime_root_source == "env"
