"""Functional core for spawn capability inventory and snapshot replay."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.catalog.agent import AgentProfile, MeridianCapabilities
from meridian.lib.core.domain import SkillContent
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import CLAUDE_SPAWN_USAGE_VARIANTS
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_policy_snapshot
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy, TerminalSurfaceMode
from meridian.lib.launch.policy_snapshot import _snapshot_profile, replay_launch_policy_snapshot
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.launch.spawn_guidance import (
    build_guidance_blocks,
    build_spawn_usage_contract,
    has_spawn_capability,
)

CONTRACT_MARKER = "# Spawning subagents (meridian)"
INVENTORY = "## Meridian Agents\n\n- tech-lead\n- explorer"


def _profile(
    *,
    subagents: tuple[str, ...] = ("coder", "explorer"),
    capabilities: MeridianCapabilities | None = None,
) -> AgentProfile:
    return AgentProfile(
        name="tech-lead",
        description="Orchestrator",
        mode="primary",
        skills=(),
        subagents=subagents,
        meridian_capabilities=capabilities,
        model_invocable=False,
        body="Lead body",
        path=Path("/tmp/tech-lead.md"),
        raw_content="raw profile content",
    )


def _terminal_surface_mode(*, harness_id: HarnessId) -> TerminalSurfaceMode:
    _ = harness_id
    return TerminalSurfaceMode.PTY_MEDIATED


def test_capable_profile_gets_inventory_and_spawn_contract() -> None:
    blocks = build_guidance_blocks(
        profile=_profile(),
        spawn_usage_contract=build_spawn_usage_contract(CLAUDE_SPAWN_USAGE_VARIANTS),
        bundle_inventory_prompt=INVENTORY,
        context_prompt="",
    )

    assert [block.name for block in blocks[:3]] == [
        "inventory",
        "spawn-prompting",
        "spawn-contract",
    ]
    assert blocks[0].content == INVENTORY
    contract = next(block for block in blocks if block.name == "spawn-contract")
    assert contract.content.count(CONTRACT_MARKER) == 1
    assert "run_in_background" in contract.content


def test_spawn_capability_requires_inventory_and_honors_explicit_disable() -> None:
    cases = (
        (_profile(), True),
        (_profile(subagents=()), False),
        (_profile(capabilities=MeridianCapabilities(spawn=False)), False),
        (_profile(subagents=(), capabilities=MeridianCapabilities(spawn=True)), False),
        (None, False),
    )

    for profile, expected in cases:
        assert has_spawn_capability(profile) is expected


def test_snapshot_preserves_profile_and_raw_inventory() -> None:
    profile = _profile()
    snapshot = build_launch_policy_snapshot(
        SpawnRequest(
            prompt="replay",
            model="gpt-5.4",
            harness="claude",
            agent=profile.name,
        ),
        bundle_inventory_prompt=INVENTORY,
        profile=profile,
    )
    reconstructed = _snapshot_profile(
        snapshot=snapshot,
        snapshot_skill_names=("loaded-skill",),
        project_root=Path("/tmp/project"),
    )

    assert snapshot.bundle_inventory_prompt == INVENTORY
    assert CONTRACT_MARKER not in (snapshot.bundle_inventory_prompt or "")
    assert reconstructed is not None
    assert reconstructed.subagents == profile.subagents
    assert reconstructed.meridian_capabilities == profile.meridian_capabilities
    assert reconstructed.mode == profile.mode
    assert reconstructed.model_invocable == profile.model_invocable
    assert reconstructed.skills == ("loaded-skill",)


def test_snapshot_replay_uses_persisted_policy_inventory() -> None:
    skill = SkillContent(
        name="testing",
        description="",
        content="# Testing",
        path=".mars/skills/testing/SKILL.md",
    )
    snapshot = LaunchPolicySnapshot(
        model="gpt-5.4",
        harness="codex",
        agent="tech-lead",
        skills=("stale-live-name",),
        loaded_skills=(skill,),
        execution_policy=ResolvedExecutionPolicy(effort="high", approval="auto"),
        model_selection_selected_token="gpt54",
        model_selection_canonical_id="gpt-5.4",
    )

    replayed = replay_launch_policy_snapshot(
        snapshot=snapshot,
        project_root=Path("/tmp/project"),
        harness_registry=get_default_harness_registry(),
        skills_readonly=True,
        alias_catalog={},
        resolve_terminal_surface_mode=_terminal_surface_mode,
    )

    assert replayed.model == "gpt-5.4"
    assert replayed.routing.model == "gpt54"
    assert replayed.resolved_skills.skill_names == ("testing",)
    assert replayed.resolved_skills.loaded_skills == (skill,)
    assert replayed.execution_policy.effort == "high"
    assert replayed.execution_policy.approval == "auto"


def test_snapshot_replay_rejects_empty_harness() -> None:
    with pytest.raises(ValueError, match="missing harness"):
        replay_launch_policy_snapshot(
            snapshot=LaunchPolicySnapshot(model="gpt-5.4", harness=""),
            project_root=Path("/tmp/project"),
            harness_registry=get_default_harness_registry(),
            skills_readonly=True,
            alias_catalog={},
            resolve_terminal_surface_mode=_terminal_surface_mode,
        )
