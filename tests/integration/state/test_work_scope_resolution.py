"""Integration regression: named work scope resolves via [context.work], not runtime fallbacks."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.state.user_paths import get_project_home

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_named_active_work_resolves_via_context_work_not_runtime_fallback(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Named work items must resolve under [context.work], never runtime-root work/."""

    project_root = tmp_path / "repo"
    context_work_root = tmp_path / "context-work"
    user_home = tmp_path / "user-home"
    project_id = "proj-scope-integration"
    work_id = "feature-x"

    project_root.mkdir()
    context_work_root.mkdir()
    user_home.mkdir()

    state_dir = project_root / ".meridian"
    state_dir.mkdir()
    (state_dir / "id").write_text(project_id, encoding="utf-8")
    (project_root / "meridian.local.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                f'path = "{context_work_root.as_posix()}"',
                'archive = "ctx/archive/work"',
                "",
                "[context.kb]",
                'path = "ctx/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", work_id)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)

    runtime_root = get_project_home(project_id)
    repo_work_leak = state_dir / "work" / work_id
    runtime_work_leak = runtime_root / "work" / work_id
    repo_work_leak.mkdir(parents=True)
    runtime_work_leak.mkdir(parents=True)

    ctx = ResolvedContext.from_environment()

    expected = context_work_root / work_id
    assert ctx.work_id == work_id
    assert ctx.work_scope is not None
    assert ctx.work_scope.kind == "work_item"
    assert ctx.work_scope.identifier == work_id
    assert ctx.work_dir == expected
    assert ctx.work_dir is not None
    assert expected.parent == context_work_root
    assert ctx.work_dir != repo_work_leak.resolve()
    assert ctx.work_dir != runtime_work_leak.resolve()
    assert state_dir / "work" not in ctx.work_dir.parents
    assert runtime_root / "work" not in ctx.work_dir.parents

    overrides = ctx.child_env_overrides()
    assert overrides["MERIDIAN_ACTIVE_WORK_ID"] == work_id
    assert overrides["MERIDIAN_ACTIVE_WORK_DIR"] == expected.as_posix()
