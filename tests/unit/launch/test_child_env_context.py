"""Unit tests for ChildEnvContext resolution and projection."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.launch.context import ChildEnvContext


def _project_paths(tmp_path: Path) -> ProjectConfigPaths:
    execution_cwd = tmp_path / "child-cwd"
    execution_cwd.mkdir()
    return ProjectConfigPaths(project_root=tmp_path, execution_cwd=execution_cwd)


def _default_context_dirs(project_root: Path) -> tuple[tuple[str, Path], ...]:
    return (
        ("work", (project_root / ".meridian" / "work").resolve()),
        ("work_archive", (project_root / ".meridian" / "archive" / "work").resolve()),
        ("kb", (project_root / ".meridian" / "kb").resolve()),
    )


def test_child_env_context_from_environment_uses_resolved_context_parent_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_paths = _project_paths(tmp_path)
    runtime_state_root = tmp_path / "runtime-state"
    runtime_state_root.mkdir()
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "work-explicit")

    def fake_from_environment(cls, **kwargs: object) -> ResolvedContext:
        _ = cls
        assert kwargs["explicit_project_root"] == project_paths.project_root.resolve()
        assert kwargs["explicit_runtime_root"] == runtime_state_root.resolve()
        return ResolvedContext(depth=3, chat_id=" parent-chat ")

    monkeypatch.setattr(ResolvedContext, "from_environment", classmethod(fake_from_environment))

    resolved = ChildEnvContext.from_environment(
        project_paths=project_paths,
        runtime_root=runtime_state_root,
    )

    assert resolved == ChildEnvContext(
        parent_spawn_id=None,
        project_root=project_paths.project_root.resolve(),
        runtime_root=runtime_state_root.resolve(),
        parent_chat_id="parent-chat",
        parent_depth=3,
        work_id="work-explicit",
        work_dir=(project_paths.project_root / ".meridian" / "work" / "work-explicit").resolve(),
        context_dirs=_default_context_dirs(project_paths.project_root),
    )


def test_child_env_context_from_environment_falls_back_to_session_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_paths = _project_paths(tmp_path)
    runtime_state_root = tmp_path / "runtime-state"
    runtime_state_root.mkdir()
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)

    seen_lookup: list[tuple[Path, str]] = []

    def fake_from_environment(cls, **kwargs: object) -> ResolvedContext:
        _ = cls
        assert kwargs["explicit_project_root"] == project_paths.project_root.resolve()
        assert kwargs["explicit_runtime_root"] == runtime_state_root.resolve()
        return ResolvedContext(depth=1, chat_id="chat-lookup")

    def fake_get_session_active_work_id(runtime_root: Path, chat_id: str) -> str | None:
        seen_lookup.append((runtime_root, chat_id))
        return "work-session"

    monkeypatch.setattr(ResolvedContext, "from_environment", classmethod(fake_from_environment))
    monkeypatch.setattr(
        "meridian.lib.launch.context.get_session_active_work_id",
        fake_get_session_active_work_id,
    )

    resolved = ChildEnvContext.from_environment(
        project_paths=project_paths,
        runtime_root=runtime_state_root,
    )

    assert seen_lookup == [(runtime_state_root.resolve(), "chat-lookup")]
    assert resolved.work_id == "work-session"
    assert resolved.work_dir == (
        project_paths.project_root / ".meridian" / "work" / "work-session"
    ).resolve()
    assert resolved.context_dirs == _default_context_dirs(project_paths.project_root)


def test_child_env_context_from_environment_ignores_session_lookup_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_paths = _project_paths(tmp_path)
    runtime_state_root = tmp_path / "runtime-state"
    runtime_state_root.mkdir()
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)

    def fake_from_environment(cls, **kwargs: object) -> ResolvedContext:
        _ = cls
        assert kwargs["explicit_project_root"] == project_paths.project_root.resolve()
        assert kwargs["explicit_runtime_root"] == runtime_state_root.resolve()
        return ResolvedContext(depth=2, chat_id="chat-lookup")

    def raising_lookup(runtime_root: Path, chat_id: str) -> str | None:
        _ = (runtime_root, chat_id)
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(ResolvedContext, "from_environment", classmethod(fake_from_environment))
    monkeypatch.setattr(
        "meridian.lib.launch.context.get_session_active_work_id",
        raising_lookup,
    )

    resolved = ChildEnvContext.from_environment(
        project_paths=project_paths,
        runtime_root=runtime_state_root,
    )

    assert resolved.work_id is None
    assert resolved.work_dir is None
    assert resolved.context_dirs == _default_context_dirs(project_paths.project_root)


def test_child_env_context_keeps_repo_root_when_execution_cwd_is_spawn_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    execution_cwd = tmp_path / "runtime" / "spawns" / "p123"
    project_root.mkdir()
    execution_cwd.mkdir(parents=True)
    project_paths = ProjectConfigPaths(project_root=project_root, execution_cwd=execution_cwd)
    runtime_state_root = tmp_path / "runtime"
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "nested-spawn")

    def fake_from_environment(cls, **kwargs: object) -> ResolvedContext:
        _ = cls
        assert kwargs["explicit_project_root"] == project_root.resolve()
        assert kwargs["explicit_runtime_root"] == runtime_state_root.resolve()
        return ResolvedContext(depth=2, chat_id="parent-chat")

    monkeypatch.setattr(ResolvedContext, "from_environment", classmethod(fake_from_environment))

    resolved = ChildEnvContext.from_environment(
        project_paths=project_paths,
        runtime_root=runtime_state_root,
    )
    env = resolved.child_context(child_spawn_id="p124")

    assert resolved.project_root == project_root.resolve()
    assert resolved.work_dir == (project_root / ".meridian" / "work" / "nested-spawn").resolve()
    assert resolved.context_dirs == _default_context_dirs(project_root)
    assert env["MERIDIAN_PROJECT_DIR"] == project_root.resolve().as_posix()
    assert env["MERIDIAN_ACTIVE_WORK_DIR"] == (
        project_root / ".meridian" / "work" / "nested-spawn"
    ).resolve().as_posix()
    assert env["MERIDIAN_CONTEXT_WORK_DIR"] == (
        project_root / ".meridian" / "work"
    ).resolve().as_posix()


def test_child_env_context_child_context_produces_correct_overrides(
    tmp_path: Path,
) -> None:
    ctx = ChildEnvContext(
        parent_spawn_id=None,
        project_root=tmp_path / "repo",
        runtime_root=tmp_path / "runtime-state",
        parent_chat_id="chat-parent",
        parent_depth=5,
        work_id="work-55",
        work_dir=tmp_path / "repo/.meridian/work/work-55",
        context_dirs=(
            ("work", tmp_path / "repo/.meridian/work"),
            ("kb", tmp_path / "repo/.meridian/kb"),
            ("docs", tmp_path / "repo/.meridian/docs"),
        ),
    )

    result = ctx.child_context()

    assert result == {
        "MERIDIAN_DEPTH": "6",
        "MERIDIAN_PROJECT_DIR": ctx.project_root.as_posix(),
        "MERIDIAN_RUNTIME_DIR": ctx.runtime_root.as_posix(),
        "MERIDIAN_CHAT_ID": "chat-parent",
        "MERIDIAN_ACTIVE_WORK_ID": "work-55",
        "MERIDIAN_ACTIVE_WORK_DIR": (tmp_path / "repo/.meridian/work/work-55").as_posix(),
        "MERIDIAN_CONTEXT_WORK_DIR": (tmp_path / "repo/.meridian/work").as_posix(),
        "MERIDIAN_CONTEXT_KB_DIR": (tmp_path / "repo/.meridian/kb").as_posix(),
        "MERIDIAN_CONTEXT_DOCS_DIR": (tmp_path / "repo/.meridian/docs").as_posix(),
    }


def test_child_env_context_can_preserve_depth_for_primary_surface(
    tmp_path: Path,
) -> None:
    ctx = ChildEnvContext(
        parent_spawn_id=None,
        project_root=tmp_path / "repo",
        runtime_root=tmp_path / "runtime-state",
        parent_chat_id="chat-parent",
        parent_depth=0,
    )

    result = ctx.child_context(child_spawn_id="p-primary", increment_depth=False)

    assert result["MERIDIAN_DEPTH"] == "0"
    assert result["MERIDIAN_SPAWN_ID"] == "p-primary"
    assert "MERIDIAN_PARENT_SPAWN_ID" not in result


def test_child_env_context_passes_child_spawn_id_through(
    tmp_path: Path,
) -> None:
    ctx = ChildEnvContext(
        parent_spawn_id="p-parent",
        project_root=tmp_path / "repo",
        runtime_root=tmp_path / "runtime-state",
        parent_chat_id=None,
        parent_depth=1,
    )

    result = ctx.child_context(child_spawn_id="p2")

    assert result["MERIDIAN_SPAWN_ID"] == "p2"
    assert result["MERIDIAN_PARENT_SPAWN_ID"] == "p-parent"
    assert result["MERIDIAN_DEPTH"] == "2"
