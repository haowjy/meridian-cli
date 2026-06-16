"""ResolvedContext runtime-root derivation from project identity."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.ops.runtime import resolve_runtime_authority_for_read
from meridian.lib.state.paths import resolve_ambient_work_dir
from meridian.lib.state.runtime_root import derive_runtime_root_from_project
from meridian.lib.state.user_paths import get_project_home

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_derive_runtime_root_uses_project_id_user_home(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-abc", encoding="utf-8")

    derived = derive_runtime_root_from_project(project_root)

    assert derived == get_project_home("proj-abc")


def test_derive_runtime_root_falls_back_to_local_state_without_id(tmp_path: Path) -> None:
    project_root = tmp_path / "plain"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "spawns.jsonl").write_text("", encoding="utf-8")

    derived = derive_runtime_root_from_project(project_root)

    assert derived == state_dir.resolve()


def test_from_environment_derives_runtime_from_project_dir_not_runtime_env(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-env", encoding="utf-8")
    wrong_runtime = tmp_path / "wrong-runtime"
    wrong_runtime.mkdir()

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", wrong_runtime.as_posix())

    ctx = ResolvedContext.from_environment()

    assert ctx.project_root == project_root.resolve()
    assert ctx.runtime_root == get_project_home("proj-env")
    assert ctx.runtime_root != wrong_runtime.resolve()


def test_child_env_overrides_propagate_project_dir_only(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"

    ctx = ResolvedContext(
        project_root=project_root,
        runtime_root=runtime_root,
    )
    overrides = ctx.child_env_overrides()

    assert overrides["MERIDIAN_PROJECT_DIR"] == project_root.as_posix()
    assert "MERIDIAN_RUNTIME_DIR" not in overrides


def test_resolve_runtime_authority_ignores_runtime_env_when_flagged(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-flag", encoding="utf-8")
    override = tmp_path / "override"
    override.mkdir()
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", override.as_posix())

    authority = resolve_runtime_authority_for_read(
        project_root,
        ignore_runtime_env=True,
    )

    assert authority.runtime_root == get_project_home("proj-flag")


def test_resolve_runtime_authority_derives_when_runtime_dir_absent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """When -C scope unsets MERIDIAN_RUNTIME_DIR, runtime derives from project identity."""
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-explicit", encoding="utf-8")
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)

    authority = resolve_runtime_authority_for_read(project_root)

    assert authority.runtime_root == get_project_home("proj-explicit")


def test_resolve_runtime_authority_honors_runtime_env_at_root(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    override = tmp_path / "override"
    override.mkdir()
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", override.as_posix())

    authority = resolve_runtime_authority_for_read(project_root)

    assert authority.runtime_root == override.resolve()


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
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", override.as_posix())

    authority = resolve_runtime_authority_for_read(project_root)

    assert authority.runtime_root == override.resolve()
    assert authority.runtime_root_source == "env"


def test_from_environment_ambient_work_dir_when_no_named_work(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-ambient", encoding="utf-8")

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p42")
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)

    ctx = ResolvedContext.from_environment()

    expected = resolve_ambient_work_dir(project_root, "p42")
    assert ctx.work_id is None
    assert ctx.work_dir is not None
    assert ctx.work_dir == expected
    assert ctx.work_dir.as_posix().endswith("spawns/p42/work")


def test_from_environment_explicit_work_id_wins_over_ambient(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-named", encoding="utf-8")

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p99")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "my-feature")

    ctx = ResolvedContext.from_environment()

    assert ctx.work_id == "my-feature"
    assert ctx.work_dir is not None
    assert ctx.work_dir.name == "my-feature"
    assert "spawns/p99/work" not in ctx.work_dir.as_posix()


def test_from_environment_honors_active_work_dir_env_without_work_id(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-env-dir", encoding="utf-8")
    explicit_dir = tmp_path / "custom-scope"

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p7")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", explicit_dir.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)

    ctx = ResolvedContext.from_environment()

    assert ctx.work_id is None
    assert ctx.work_dir == explicit_dir


def test_from_environment_session_work_id_still_resolves_named_dir(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-session", encoding="utf-8")
    runtime_root = get_project_home("proj-session")

    backend = MagicMock()
    backend.get_session_active_work_id.return_value = "session-work"

    def _resolve_scratch_dir(root: Path, work_id: str) -> Path:
        return root / "work" / work_id

    backend.resolve_work_scratch_dir.side_effect = _resolve_scratch_dir

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_CHAT_ID", "chat-1")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p3")
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_DIR", raising=False)

    ctx = ResolvedContext.from_environment(backend=backend)

    assert ctx.work_id == "session-work"
    assert ctx.work_dir is not None
    assert ctx.work_dir.name == "session-work"
    backend.get_session_active_work_id.assert_called_once_with(runtime_root, "chat-1")
