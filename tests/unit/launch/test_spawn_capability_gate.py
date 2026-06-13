"""Spawn capability gate, contract injection, and snapshot round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.catalog.agent import AgentProfile
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.context import (
    PreparedLaunchSurface,
    _has_spawn_capability,
    _resolve_inventory_and_context_prompts,
    _spawn_usage_contract,
    build_launch_policy_snapshot,
    compile_prepared_policy_surface,
    prepare_launch_surface,
)
from meridian.lib.launch.policy_snapshot import _snapshot_profile
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)
from tests.support.fixtures import allow_headless_claude
from tests.support.launch import stub_bundle_request_and_resolve

CONTRACT_MARKER = "# Spawning subagents (meridian)"
PRE_GATE_INVENTORY = "## Meridian Agents\n\n- tech-lead\n- explorer"
WAIT_GUIDANCE = "meridian spawn wait"


def _sample_profile(
    *,
    subagents: tuple[str, ...] = ("coder", "explorer"),
    meridian_capabilities: dict[str, bool] | None = None,
) -> AgentProfile:
    return AgentProfile(
        name="tech-lead",
        description="Orchestrator",
        skills=(),
        subagents=subagents,
        meridian_capabilities=meridian_capabilities,
        body="Lead body",
        path=Path("/tmp/tech-lead.md"),
        raw_content="raw",
    )


def _seed_spawn_capable_project(project_root: Path) -> None:
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    allow_headless_claude(project_root)
    agent_path = project_root / ".mars" / "agents" / "tech-lead.md"
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_text(
        "\n".join(
            [
                "---",
                "name: tech-lead",
                "model: claude-sonnet-4-6",
                "subagents: [coder, explorer]",
                "skills: []",
                "---",
                "",
                "# Tech Lead",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _spawn_prepare_runtime(project_root: Path) -> LaunchRuntime:
    return LaunchRuntime(
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        runtime_root=(project_root / ".meridian").as_posix(),
        project_paths_project_root=project_root.as_posix(),
        project_paths_execution_cwd=project_root.as_posix(),
    )


def _compose_spawn_prepare_surface(
    *,
    project_root: Path,
    request: SpawnRequest,
) -> PreparedLaunchSurface:
    runtime = _spawn_prepare_runtime(project_root)
    catalog = CatalogSession(project_root)
    prepared_policy = compile_prepared_policy_surface(
        request=request,
        runtime=runtime,
        project_root=project_root,
        harness_registry=get_default_harness_registry(),
        catalog=catalog,
    )
    return prepare_launch_surface(
        request=request,
        runtime=runtime,
        prepared_policy=prepared_policy,
    )


def _composed_system_prompt(prepared: PreparedLaunchSurface) -> str:
    return prepared.content.prompt_payload.appended_system_prompt or ""


def _replay_request_from_snapshot(snapshot: LaunchPolicySnapshot, *, prompt: str) -> SpawnRequest:
    return SpawnRequest(
        prompt=prompt,
        prompt_is_composed=False,
        model=snapshot.model,
        harness=snapshot.harness,
        agent=snapshot.agent,
        skills=snapshot.skills,
        execution_policy=snapshot.execution_policy,
        launch_policy_snapshot=snapshot,
    )


def test_gate_present_injects_contract_into_inventory(tmp_path: Path) -> None:
    inventory, context = _resolve_inventory_and_context_prompts(
        project_root=tmp_path,
        active_work_dir=None,
        bundle_inventory_prompt=PRE_GATE_INVENTORY,
        has_spawn_capability=True,
        harness_id=HarnessId.CLAUDE,
    )

    assert inventory is not None
    assert inventory.startswith(PRE_GATE_INVENTORY)
    assert inventory.count(CONTRACT_MARKER) == 1
    assert "run_in_background" in inventory
    assert context is not None
    assert "# Meridian Context" in context


def test_gate_absent_suppresses_inventory_but_keeps_context(tmp_path: Path) -> None:
    inventory, context = _resolve_inventory_and_context_prompts(
        project_root=tmp_path,
        active_work_dir=None,
        bundle_inventory_prompt=PRE_GATE_INVENTORY,
        has_spawn_capability=False,
    )

    assert inventory is None
    assert context is not None
    assert "# Meridian Context" in context


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (_sample_profile(), True),
        (_sample_profile(subagents=()), False),
        (_sample_profile(meridian_capabilities={"spawn": False}), False),
        (_sample_profile(meridian_capabilities={"spawn": True}), True),
        (None, False),
    ],
)
def test_has_spawn_capability(profile: AgentProfile | None, expected: bool) -> None:
    assert _has_spawn_capability(profile) is expected


def test_spawn_usage_contract_is_harness_templated() -> None:
    claude = _spawn_usage_contract(HarnessId.CLAUDE)
    generic = _spawn_usage_contract(HarnessId.CODEX)

    assert "run_in_background" in claude
    assert "run_in_background" not in generic
    assert CONTRACT_MARKER in claude
    assert CONTRACT_MARKER in generic
    assert WAIT_GUIDANCE in claude
    assert WAIT_GUIDANCE in generic


def test_snapshot_round_trip_preserves_spawn_gate_fields() -> None:
    profile = _sample_profile(meridian_capabilities={"spawn": False, "notify": True})
    snapshot = build_launch_policy_snapshot(
        SpawnRequest(prompt="replay", model="gpt55", harness="claude", agent="tech-lead"),
        bundle_inventory_prompt=PRE_GATE_INVENTORY,
        profile=profile,
    )

    reconstructed = _snapshot_profile(
        snapshot=snapshot,
        project_root=Path("/tmp/project"),
        snapshot_agent="tech-lead",
        snapshot_skill_names=(),
    )

    assert reconstructed is not None
    assert reconstructed.subagents == profile.subagents
    assert reconstructed.meridian_capabilities == profile.meridian_capabilities


def _empty_alias_map(_self: CatalogSession) -> dict[str, object]:
    return {}


def _block_launch_bundle(
    _request: bundle_adapter.BundleRequest,
    *,
    harness_registry: object,
) -> None:
    _ = harness_registry
    raise AssertionError("snapshot replay should not call launch-bundle")


def test_continue_fork_replay_appends_contract_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose → snapshot (pre-gate) → replay via policy snapshot → gate once."""
    project_root = tmp_path
    _seed_spawn_capable_project(project_root)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE,
        prompt_surface_inventory_prompt=PRE_GATE_INVENTORY,
    )
    monkeypatch.setattr(CatalogSession, "alias_map", _empty_alias_map)

    initial_request = SpawnRequest(
        prompt="initial task",
        prompt_is_composed=False,
        model="claude-sonnet-4-6",
        harness="claude",
        agent="tech-lead",
    )
    initial_prepared = _compose_spawn_prepare_surface(
        project_root=project_root,
        request=initial_request,
    )
    snapshot = initial_prepared.request.launch_policy_snapshot
    assert snapshot is not None
    assert snapshot.bundle_inventory_prompt == PRE_GATE_INVENTORY
    assert CONTRACT_MARKER not in (snapshot.bundle_inventory_prompt or "")

    initial_system_prompt = _composed_system_prompt(initial_prepared)
    assert initial_system_prompt.count(CONTRACT_MARKER) == 1
    assert initial_system_prompt.count(PRE_GATE_INVENTORY) == 1

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _block_launch_bundle)

    replay_request = _replay_request_from_snapshot(snapshot, prompt="continue task")
    first_replay = _compose_spawn_prepare_surface(
        project_root=project_root,
        request=replay_request,
    )
    first_replay_prompt = _composed_system_prompt(first_replay)
    assert first_replay_prompt.count(CONTRACT_MARKER) == 1
    assert first_replay_prompt.count(PRE_GATE_INVENTORY) == 1

    second_replay = _compose_spawn_prepare_surface(
        project_root=project_root,
        request=_replay_request_from_snapshot(snapshot, prompt="continue again"),
    )
    second_replay_prompt = _composed_system_prompt(second_replay)
    assert second_replay_prompt.count(CONTRACT_MARKER) == 1
    assert second_replay_prompt.count(PRE_GATE_INVENTORY) == 1


def test_build_launch_policy_snapshot_stores_pre_gate_inventory_only() -> None:
    profile = _sample_profile()
    snapshot = build_launch_policy_snapshot(
        SpawnRequest(prompt="store", model="gpt55", harness="claude", agent="tech-lead"),
        bundle_inventory_prompt=PRE_GATE_INVENTORY,
        profile=profile,
    )

    assert snapshot.bundle_inventory_prompt == PRE_GATE_INVENTORY
    assert CONTRACT_MARKER not in (snapshot.bundle_inventory_prompt or "")
    assert snapshot.agent_subagents == profile.subagents
