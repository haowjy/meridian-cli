"""ResolvedContext runtime-root derivation from project identity."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.types import SpawnId
from meridian.lib.ops.runtime import (
    resolve_runtime_authority_for_read,
    resolve_runtime_authority_for_write,
)
from meridian.lib.state.runtime_root import derive_runtime_root_from_project
from meridian.lib.state.user_paths import get_project_home

if TYPE_CHECKING:
    from pytest import MonkeyPatch


class _CurrentChatBackend:
    def get_execution_chat(self, runtime_root: Path, spawn_id: SpawnId) -> str | None:
        _ = runtime_root, spawn_id
        return "c-current"

    def get_session_active_work_id(self, runtime_root: Path, chat_id: str) -> str | None:
        _ = runtime_root
        return {"c-current": "work-current", "c-explicit": "work-explicit"}.get(chat_id)


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


def test_adhoc_authority_reports_would_be_id_and_creation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "scratch"
    project_root.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("MERIDIAN_HOME", home.as_posix())

    read = resolve_runtime_authority_for_read(project_root)
    first_write = resolve_runtime_authority_for_write(project_root)
    second_write = resolve_runtime_authority_for_write(project_root)

    assert read.identity_tier == "adhoc"
    assert read.project_id is not None
    assert read.project_id.startswith("adhoc.scratch.")
    assert read.runtime_root is None
    assert not read.identity_created
    assert first_write.runtime_root == home / "projects" / read.project_id
    assert first_write.identity_created
    assert not second_write.identity_created
    assert list(project_root.iterdir()) == []


def test_runtime_override_bypasses_adhoc_materialization(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "scratch"
    project_root.mkdir()
    home = tmp_path / "home"
    override = tmp_path / "override"
    monkeypatch.setenv("MERIDIAN_HOME", home.as_posix())
    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", override.as_posix())
    monkeypatch.delenv("_MERIDIAN_DEPTH", raising=False)

    authority = resolve_runtime_authority_for_write(project_root)

    assert authority.runtime_root == override
    assert not authority.identity_created
    assert not home.exists()
    assert list(project_root.iterdir()) == []


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


def test_ambient_context_follows_the_execution_chat_but_explicit_chat_does_not(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    project_root = tmp_path / "project"
    runtime_root.mkdir()
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p1")
    monkeypatch.setenv("MERIDIAN_CHAT_ID", "c-entry")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "work-stale")

    ambient = ResolvedContext.from_environment(
        explicit_project_root=project_root,
        explicit_runtime_root=runtime_root,
        backend=_CurrentChatBackend(),
    )
    explicit = ResolvedContext.from_environment(
        explicit_chat_id="c-explicit",
        explicit_project_root=project_root,
        explicit_runtime_root=runtime_root,
        backend=_CurrentChatBackend(),
    )

    assert (ambient.chat_id, ambient.work_id) == ("c-current", "work-current")
    assert (explicit.chat_id, explicit.work_id) == ("c-explicit", "work-explicit")
