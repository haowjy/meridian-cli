"""Adapter-owned spawn usage contract variants."""

from __future__ import annotations

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import (
    CLAUDE_SPAWN_USAGE_VARIANTS,
    GENERIC_SPAWN_USAGE_VARIANTS,
)
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.spawn_guidance import build_spawn_usage_contract

CONTRACT_MARKER = "# Spawning subagents (meridian)"


def test_each_registered_harness_has_nonempty_spawn_contract() -> None:
    registry = get_default_harness_registry()
    for harness_id in registry.ids():
        adapter = registry.get_subprocess_harness(harness_id)
        contract = build_spawn_usage_contract(
            adapter.run_prompt_policy().spawn_usage_contract_variants
        )
        assert contract.strip(), f"{harness_id} spawn contract is empty"
        assert CONTRACT_MARKER in contract


def test_claude_and_generic_spawn_contracts_differ_by_adapter_data() -> None:
    registry = get_default_harness_registry()
    claude = build_spawn_usage_contract(
        registry.get_subprocess_harness(HarnessId.CLAUDE)
        .run_prompt_policy()
        .spawn_usage_contract_variants
    )
    generic = build_spawn_usage_contract(
        registry.get_subprocess_harness(HarnessId.CODEX)
        .run_prompt_policy()
        .spawn_usage_contract_variants
    )

    assert claude != generic
    assert "run_in_background" in claude
    assert "run_in_background" not in generic
    assert "harness's background execution" in generic
    assert claude == build_spawn_usage_contract(CLAUDE_SPAWN_USAGE_VARIANTS)
    assert generic == build_spawn_usage_contract(GENERIC_SPAWN_USAGE_VARIANTS)


def test_non_claude_harnesses_share_generic_spawn_contract_variants() -> None:
    registry = get_default_harness_registry()
    for harness_id in registry.ids():
        if harness_id == HarnessId.CLAUDE:
            continue
        adapter = registry.get_subprocess_harness(harness_id)
        assert (
            adapter.run_prompt_policy().spawn_usage_contract_variants
            == GENERIC_SPAWN_USAGE_VARIANTS
        )
