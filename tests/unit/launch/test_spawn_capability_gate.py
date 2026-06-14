"""Spawn capability gate, contract injection, and snapshot round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.catalog.agent import AgentProfile, MeridianCapabilities
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.context import (
    PreparedLaunchSurface,
    build_context_prompt,
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
from meridian.lib.launch.spawn_guidance import has_spawn_capability, resolve_spawn_prompt_blocks
from tests.support.fixtures import allow_headless_claude
from tests.support.launch import stub_bundle_request_and_resolve

CONTRACT_MARKER = "# Spawning subagents (meridian)"
PRE_GATE_INVENTORY = "## Meridian Agents\n\n- tech-lead\n- explorer"
WAIT_GUIDANCE = "meridian spawn wait"


def _sample_profile(
    *,
    subagents: tuple[str, ...] = ("coder", "explorer"),
    meridian_capabilities: MeridianCapabilities | None = None,
    mode: str = "subagent",
    model_invocable: bool = True,
) -> AgentProfile:
    return AgentProfile(
        name="tech-lead",
        description="Orchestrator",
        mode=mode,  # type: ignore[arg-type]
        skills=(),
        subagents=subagents,
        meridian_capabilities=meridian_capabilities,
        model_invocable=model_invocable,
        body="Lead body",
        path=Path("/tmp/tech-lead.md"),
        raw_content="raw profile content",
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


def test_gate_present_returns_inventory_and_contract_blocks(tmp_path: Path) -> None:
    inventory, contract = resolve_spawn_prompt_blocks(
        profile=_sample_profile(),
        harness_id=HarnessId.CLAUDE,
        bundle_inventory_prompt=PRE_GATE_INVENTORY,
    )

    assert inventory == PRE_GATE_INVENTORY
    assert contract.count(CONTRACT_MARKER) == 1
    assert "run_in_background" in contract
    context = build_context_prompt(project_root=tmp_path, active_work_dir=None)
    assert "# Meridian Context" in context


def test_gate_absent_returns_empty_blocks_but_context_still_builds(tmp_path: Path) -> None:
    inventory, contract = resolve_spawn_prompt_blocks(
        profile=_sample_profile(subagents=()),
        harness_id=HarnessId.CLAUDE,
        bundle_inventory_prompt=PRE_GATE_INVENTORY,
    )

    assert inventory == ""
    assert contract == ""
    context = build_context_prompt(project_root=tmp_path, active_work_dir=None)
    assert "# Meridian Context" in context


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (_sample_profile(), True),
        (_sample_profile(subagents=()), False),
        (_sample_profile(meridian_capabilities=MeridianCapabilities(spawn=False)), False),
        (_sample_profile(meridian_capabilities=MeridianCapabilities(spawn=True)), True),
        (None, False),
    ],
)
def test_has_spawn_capability(profile: AgentProfile | None, expected: bool) -> None:
    assert has_spawn_capability(profile) is expected


def test_resolve_spawn_prompt_blocks_is_harness_templated() -> None:
    _, claude_contract = resolve_spawn_prompt_blocks(
        profile=_sample_profile(),
        harness_id=HarnessId.CLAUDE,
        bundle_inventory_prompt=None,
    )
    _, generic_contract = resolve_spawn_prompt_blocks(
        profile=_sample_profile(),
        harness_id=HarnessId.CODEX,
        bundle_inventory_prompt=None,
    )

    assert "run_in_background" in claude_contract
    assert "run_in_background" not in generic_contract
    assert CONTRACT_MARKER in claude_contract
    assert CONTRACT_MARKER in generic_contract
    assert WAIT_GUIDANCE in claude_contract
    assert WAIT_GUIDANCE in generic_contract


def test_snapshot_round_trip_preserves_spawn_gate_fields() -> None:
    profile = _sample_profile(meridian_capabilities=MeridianCapabilities(spawn=False))
    snapshot = build_launch_policy_snapshot(
        SpawnRequest(prompt="replay", model="gpt55", harness="claude", agent="tech-lead"),
        bundle_inventory_prompt=PRE_GATE_INVENTORY,
        profile=profile,
    )

    reconstructed = _snapshot_profile(
        snapshot=snapshot,
        snapshot_skill_names=(),
        project_root=Path("/tmp/project"),
    )

    assert reconstructed is not None
    assert reconstructed.subagents == profile.subagents
    assert reconstructed.meridian_capabilities == profile.meridian_capabilities


def test_snapshot_round_trip_preserves_all_profile_fields() -> None:
    profile = _sample_profile(
        subagents=("coder", "reviewer"),
        meridian_capabilities=MeridianCapabilities(spawn=True),
        mode="primary",
        model_invocable=False,
    )
    snapshot = build_launch_policy_snapshot(
        SpawnRequest(prompt="replay", model="gpt55", harness="claude", agent="tech-lead"),
        bundle_inventory_prompt=PRE_GATE_INVENTORY,
        profile=profile,
    )

    reconstructed = _snapshot_profile(
        snapshot=snapshot,
        snapshot_skill_names=("loaded-skill",),
        project_root=Path("/tmp/project"),
    )

    assert reconstructed is not None
    assert reconstructed.description == profile.description
    assert reconstructed.body == profile.body
    assert reconstructed.subagents == profile.subagents
    assert reconstructed.meridian_capabilities == profile.meridian_capabilities
    assert reconstructed.mode == profile.mode
    assert reconstructed.model_invocable == profile.model_invocable
    assert reconstructed.raw_content == profile.raw_content
    # Reconstruction resolves the persisted path (policy_snapshot resolves the
    # session_agent_path). On POSIX an absolute path resolves to itself; on
    # Windows resolve() anchors the current drive, so compare against the
    # resolved form rather than the raw original.
    assert reconstructed.path == profile.path.expanduser().resolve()
    assert reconstructed.skills == ("loaded-skill",)


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
    """Compose → snapshot (raw inventory) → replay via policy snapshot → gate once."""
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
    assert CONTRACT_MARKER not in (initial_prepared.content.agent_inventory_prompt or "")

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


def test_build_launch_policy_snapshot_stores_raw_inventory_only() -> None:
    profile = _sample_profile()
    snapshot = build_launch_policy_snapshot(
        SpawnRequest(prompt="store", model="gpt55", harness="claude", agent="tech-lead"),
        bundle_inventory_prompt=PRE_GATE_INVENTORY,
        profile=profile,
    )

    assert snapshot.bundle_inventory_prompt == PRE_GATE_INVENTORY
    assert CONTRACT_MARKER not in (snapshot.bundle_inventory_prompt or "")
    persisted = AgentProfile.model_validate(snapshot.agent_profile)
    assert persisted.subagents == profile.subagents
