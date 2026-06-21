"""Integration coverage for agent-relative subagent listing."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.ops.spawn.api import SpawnSubagentsInput, spawn_subagents_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root

pytestmark = pytest.mark.slow

_ALL_SUBAGENTS = ("coder", "prober", "reviewer")
_ALLOWLIST = ("coder", "reviewer")


def _seed_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (project_root / ".meridian").mkdir(parents=True)
    return project_root


def _seed_spawn(
    project_root: Path,
    *,
    spawn_id: str,
    agent: str,
) -> None:
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="gpt-5.4",
        agent=agent,
        harness="cursor",
        prompt="task",
    )


def test_spawn_subagents_uses_explicit_allow_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    _seed_spawn(project_root, spawn_id="p1", agent="tech-lead")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p1")

    def fake_agent_subagents(root: Path, agent_name: str) -> tuple[str, ...] | None:
        assert root == project_root
        assert agent_name == "tech-lead"
        return _ALLOWLIST

    def fail_list_subagents(root: Path) -> tuple[str, ...]:
        raise AssertionError(f"mars_list_subagents should not run: {root}")

    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.mars_agent_subagents",
        fake_agent_subagents,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.mars_list_subagents",
        fail_list_subagents,
    )

    output = spawn_subagents_sync(SpawnSubagentsInput(project_root=project_root.as_posix()))

    assert output.names == _ALLOWLIST


def test_spawn_subagents_empty_allow_list_is_leaf_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    _seed_spawn(project_root, spawn_id="p2", agent="coder")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p2")

    def fake_agent_subagents(root: Path, agent_name: str) -> tuple[str, ...] | None:
        assert root == project_root
        assert agent_name == "coder"
        return ()

    def fail_list_subagents(root: Path) -> tuple[str, ...]:
        raise AssertionError(f"mars_list_subagents should not run: {root}")

    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.mars_agent_subagents",
        fake_agent_subagents,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.mars_list_subagents",
        fail_list_subagents,
    )

    output = spawn_subagents_sync(SpawnSubagentsInput(project_root=project_root.as_posix()))

    assert output.names == ()


def test_spawn_subagents_unresolved_agent_returns_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    _seed_spawn(project_root, spawn_id="p3", agent="unknown-agent")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p3")

    def fake_agent_subagents(root: Path, agent_name: str) -> tuple[str, ...] | None:
        assert root == project_root
        assert agent_name == "unknown-agent"
        return None

    def fail_list_subagents(root: Path) -> tuple[str, ...]:
        raise AssertionError(f"mars_list_subagents should not run: {root}")

    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.mars_agent_subagents",
        fake_agent_subagents,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.mars_list_subagents",
        fail_list_subagents,
    )

    output = spawn_subagents_sync(SpawnSubagentsInput(project_root=project_root.as_posix()))

    assert output.names == ()


def test_spawn_subagents_without_spawn_context_lists_all_subagents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)

    def fail_agent_subagents(root: Path, agent_name: str) -> tuple[str, ...] | None:
        raise AssertionError(f"mars_agent_subagents should not run: {root} {agent_name}")

    def fake_list_subagents(root: Path) -> tuple[str, ...]:
        assert root == project_root
        return _ALL_SUBAGENTS

    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.mars_agent_subagents",
        fail_agent_subagents,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.mars_list_subagents",
        fake_list_subagents,
    )

    output = spawn_subagents_sync(SpawnSubagentsInput(project_root=project_root.as_posix()))

    assert output.names == _ALL_SUBAGENTS
