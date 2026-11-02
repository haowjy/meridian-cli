"""Integration coverage for agent-relative subagent listing."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.ops.mars import MarsAgentEntry
from meridian.lib.ops.spawn.api import SpawnSubagentsInput, spawn_subagents_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write

pytestmark = pytest.mark.slow

# The session under test runs on cursor.
_CATALOG = (
    MarsAgentEntry(name="architect", mode="subagent", description="Compares designs."),
    # Native on the session's harness but a fanout agent: Meridian stays its route.
    MarsAgentEntry(
        name="coder",
        mode="subagent",
        description="Writes the change.",
        native_harnesses=("cursor",),
        fanout=True,
    ),
    MarsAgentEntry(
        name="manual",
        mode="subagent",
        description="Only when asked.",
        model_invocable=False,
    ),
    MarsAgentEntry(name="planner", mode="primary", description="Plans."),
    MarsAgentEntry(name="prober", scope="fallback", mode="subagent"),
    MarsAgentEntry(
        name="reviewer",
        scope="fallback",
        mode="subagent",
        description="Reviews the change.",
        native_harnesses=("cursor",),
    ),
)
_DECLARED = ("coder", "manual", "planner", "reviewer")


def _lead(subagents: tuple[str, ...]) -> MarsAgentEntry:
    return MarsAgentEntry(name="tech-lead", mode="primary", subagents=subagents)


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
    runtime_root = resolve_project_runtime_root_for_write(project_root)
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


def _patch_mars(
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    catalog: tuple[MarsAgentEntry, ...] = (*_CATALOG, _lead(_DECLARED)),
) -> None:
    def fake_list_agents(root: Path) -> tuple[MarsAgentEntry, ...]:
        assert root == project_root
        return catalog

    monkeypatch.setattr("meridian.lib.ops.spawn.api.mars_list_agents", fake_list_agents)


def test_spawn_subagents_defaults_to_the_callers_subagents_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    _seed_spawn(project_root, spawn_id="p1", agent="tech-lead")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p1")
    _patch_mars(monkeypatch, project_root)

    output = spawn_subagents_sync(SpawnSubagentsInput(project_root=project_root.as_posix()))

    # `manual` is declared but model-invocable: false, so only --all shows it.
    assert output.format_text().splitlines() == [
        "- coder: Writes the change.",
        "- planner (primary): Plans.",
        "- reviewer (native: use your harness's agent tool): Reviews the change.",
    ]


def test_spawn_subagents_all_marks_hidden_and_undeclared_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    _seed_spawn(project_root, spawn_id="p1", agent="tech-lead")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p1")
    _patch_mars(monkeypatch, project_root)

    output = spawn_subagents_sync(
        SpawnSubagentsInput(project_root=project_root.as_posix(), all=True)
    )

    assert output.format_text().splitlines() == [
        "- architect (outside your subagents list): Compares designs.",
        "- coder: Writes the change.",
        "- manual (model-invocable: false; spawn only when asked): Only when asked.",
        "- planner (primary): Plans.",
        "- prober (outside your subagents list)",
        "- reviewer (native: use your harness's agent tool): Reviews the change.",
        "- tech-lead (primary; outside your subagents list)",
    ]


def test_spawn_subagents_empty_allow_list_is_leaf_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    _seed_spawn(project_root, spawn_id="p2", agent="tech-lead")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p2")
    _patch_mars(monkeypatch, project_root, (*_CATALOG, _lead(())))

    output = spawn_subagents_sync(SpawnSubagentsInput(project_root=project_root.as_posix()))

    assert output.agents == ()
    assert output.empty == "no_declared_agents"
    assert "meridian spawn subagents --all" in output.format_text()


def test_spawn_subagents_uninstalled_caller_lists_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    _seed_spawn(project_root, spawn_id="p3", agent="tech-lead")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p3")
    _patch_mars(monkeypatch, project_root, _CATALOG)

    output = spawn_subagents_sync(SpawnSubagentsInput(project_root=project_root.as_posix()))

    assert output.empty == "no_declared_agents"


def test_spawn_subagents_reports_a_mars_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)

    def failing_list_agents(root: Path) -> tuple[MarsAgentEntry, ...]:
        raise ValueError("mars agents list failed: boom")

    monkeypatch.setattr("meridian.lib.ops.spawn.api.mars_list_agents", failing_list_agents)

    with pytest.raises(ValueError, match="boom"):
        spawn_subagents_sync(SpawnSubagentsInput(project_root=project_root.as_posix()))


def test_spawn_subagents_without_calling_agent_points_to_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    _patch_mars(monkeypatch, project_root)

    default = spawn_subagents_sync(SpawnSubagentsInput(project_root=project_root.as_posix()))
    every = spawn_subagents_sync(
        SpawnSubagentsInput(project_root=project_root.as_posix(), all=True)
    )

    assert default.agents == ()
    assert "meridian spawn subagents --all" in default.format_text()
    # With no caller there is no list to be outside of.
    assert tuple(entry.name for entry in every.agents) == (*(e.name for e in _CATALOG), "tech-lead")
    assert "outside your subagents list" not in every.format_text()
