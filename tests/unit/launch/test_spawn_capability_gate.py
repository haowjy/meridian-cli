"""Spawn capability gate, contract injection, and snapshot round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.catalog.agent import AgentProfile, MeridianCapabilities
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import (
    CLAUDE_SPAWN_USAGE_VARIANTS,
    GENERIC_SPAWN_USAGE_VARIANTS,
)
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.composition import (
    ComposedLaunchContent,
    CompositionBlock,
    GuidancePhase,
    render_system_instruction_blocks,
)
from meridian.lib.launch.context import (
    PreparedLaunchSurface,
    build_context_prompt,
    build_launch_context,
    build_launch_policy_snapshot,
    compile_prepared_policy_surface,
    prepare_launch_surface,
)
from meridian.lib.launch.launch_types import TerminalSurfaceMode
from meridian.lib.launch.policy_snapshot import _snapshot_profile, replay_launch_policy_snapshot
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)
from meridian.lib.launch.spawn_guidance import (
    build_guidance_blocks,
    build_spawn_usage_contract,
    has_spawn_capability,
)
from tests.support.fixtures import allow_headless_claude
from tests.support.launch import stub_bundle_request_and_resolve

CONTRACT_MARKER = "# Spawning subagents (meridian)"
PRE_GATE_INVENTORY = "## Meridian Agents\n\n- tech-lead\n- explorer"
WAIT_GUIDANCE = "meridian spawn wait"
SAMPLE_CONTEXT = "# Meridian Context\n\nwork: /tmp/work"


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


def _block_names(blocks: tuple[CompositionBlock, ...]) -> list[str]:
    return [block.name for block in blocks]


def test_gate_present_returns_inventory_and_contract_blocks(tmp_path: Path) -> None:
    blocks = build_guidance_blocks(
        profile=_sample_profile(),
        spawn_usage_contract=build_spawn_usage_contract(CLAUDE_SPAWN_USAGE_VARIANTS),
        bundle_inventory_prompt=PRE_GATE_INVENTORY,
        context_prompt=SAMPLE_CONTEXT,
    )
    names = _block_names(blocks)

    inventory = next(block for block in blocks if block.name == "inventory")
    contract = next(block for block in blocks if block.name == "spawn-contract")

    assert inventory.content == PRE_GATE_INVENTORY
    assert contract.content.count(CONTRACT_MARKER) == 1
    assert "run_in_background" in contract.content
    assert names == [
        "inventory",
        "spawn-prompting",
        "spawn-contract",
        "work-discovery",
        "task-dir-discovery",
        "session-discovery",
        "context-env",
    ]
    context = build_context_prompt(project_root=tmp_path, active_work_dir=None)
    assert context is not None
    assert "# Meridian Context" in context
    assert "meridian context -h" in context


def test_gate_absent_returns_empty_blocks_but_context_still_builds(tmp_path: Path) -> None:
    blocks = build_guidance_blocks(
        profile=_sample_profile(subagents=()),
        spawn_usage_contract=build_spawn_usage_contract(CLAUDE_SPAWN_USAGE_VARIANTS),
        bundle_inventory_prompt=PRE_GATE_INVENTORY,
        context_prompt=SAMPLE_CONTEXT,
    )
    names = _block_names(blocks)

    assert "inventory" not in names
    assert "spawn-contract" not in names
    assert names == [
        "work-discovery",
        "task-dir-discovery",
        "session-discovery",
        "context-env",
    ]
    context = build_context_prompt(project_root=tmp_path, active_work_dir=None)
    assert context is not None
    assert "# Meridian Context" in context
    assert "meridian context -h" in context


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
    claude_blocks = build_guidance_blocks(
        profile=_sample_profile(),
        spawn_usage_contract=build_spawn_usage_contract(CLAUDE_SPAWN_USAGE_VARIANTS),
        bundle_inventory_prompt=None,
        context_prompt="",
    )
    generic_blocks = build_guidance_blocks(
        profile=_sample_profile(),
        spawn_usage_contract=build_spawn_usage_contract(GENERIC_SPAWN_USAGE_VARIANTS),
        bundle_inventory_prompt=None,
        context_prompt="",
    )
    claude_contract = next(block for block in claude_blocks if block.name == "spawn-contract")
    generic_contract = next(block for block in generic_blocks if block.name == "spawn-contract")

    assert "run_in_background" in claude_contract.content
    assert "run_in_background" not in generic_contract.content
    assert CONTRACT_MARKER in claude_contract.content
    assert CONTRACT_MARKER in generic_contract.content
    assert WAIT_GUIDANCE in claude_contract.content
    assert WAIT_GUIDANCE in generic_contract.content


def test_guidance_blocks_render_sorted_by_phase_and_priority() -> None:
    blocks = (
        CompositionBlock("context-env", GuidancePhase.ENVIRONMENT, 0, "CONTEXT-ENV"),
        CompositionBlock("session-discovery", GuidancePhase.GUIDANCE, 21, "SESSION-DISCOVERY"),
        CompositionBlock("spawn-contract", GuidancePhase.GUIDANCE, 10, "SPAWN-CONTRACT"),
        CompositionBlock("work-discovery", GuidancePhase.GUIDANCE, 20, "WORK-DISCOVERY"),
        CompositionBlock("inventory", GuidancePhase.GUIDANCE, 0, "INVENTORY"),
    )
    content = ComposedLaunchContent(
        supplemental_documents=(),
        agent_profile_body="",
        report_instruction="",
        guidance_blocks=blocks,
        passthrough_system_fragments=(),
        user_task_prompt="",
        reference_items=(),
        prior_output="",
    )

    rendered = render_system_instruction_blocks(content)
    expected_sequence = (
        "INVENTORY",
        "SPAWN-CONTRACT",
        "WORK-DISCOVERY",
        "SESSION-DISCOVERY",
        "CONTEXT-ENV",
    )
    cursor = -1
    for part in expected_sequence:
        cursor = rendered.find(part, cursor + 1)
        assert cursor != -1, f"Expected '{part}' after position {cursor} in rendered output"


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


def test_snapshot_replay_allows_empty_model_for_opencode_without_model_flag(
    tmp_path: Path,
) -> None:
    def terminal_surface_mode(*, harness_id: HarnessId) -> TerminalSurfaceMode:
        _ = harness_id
        return TerminalSurfaceMode.PTY_MEDIATED

    snapshot = LaunchPolicySnapshot(model="", harness="opencode")
    harness_registry = get_default_harness_registry()

    replayed = replay_launch_policy_snapshot(
        snapshot=snapshot,
        project_root=tmp_path,
        harness_registry=harness_registry,
        skills_readonly=True,
        alias_catalog={},
        resolve_terminal_surface_mode=terminal_surface_mode,
    )

    assert replayed.model is None
    assert replayed.routing.model is None
    assert replayed.model_selection is None

    ctx = build_launch_context(
        spawn_id="empty-opencode-model",
        request=_replay_request_from_snapshot(snapshot, prompt="continue task"),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=harness_registry,
        dry_run=True,
    )

    assert ctx.model_selection is None
    assert ctx.binding.run_params.model is None
    assert ctx.binding.spec.model is None
    assert "--model" not in ctx.binding.argv


def test_snapshot_replay_keeps_empty_harness_invalid(tmp_path: Path) -> None:
    def terminal_surface_mode(*, harness_id: HarnessId) -> TerminalSurfaceMode:
        _ = harness_id
        return TerminalSurfaceMode.PTY_MEDIATED

    with pytest.raises(ValueError, match="missing harness"):
        replay_launch_policy_snapshot(
            snapshot=LaunchPolicySnapshot(model="gpt-5.3-codex", harness=""),
            project_root=tmp_path,
            harness_registry=get_default_harness_registry(),
            skills_readonly=True,
            alias_catalog={},
            resolve_terminal_surface_mode=terminal_surface_mode,
        )


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
