"""Unit tests for launch-policy snapshot replay normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import TerminalSurfaceMode
from meridian.lib.launch.policy_snapshot import (
    managed_model_override_from_persisted_model,
    replay_launch_policy_snapshot,
)


def test_managed_model_override_from_persisted_model_empty_is_none() -> None:
    assert managed_model_override_from_persisted_model("") is None
    assert managed_model_override_from_persisted_model("   ") is None


def test_managed_model_override_from_persisted_model_preserves_token() -> None:
    assert managed_model_override_from_persisted_model("gpt-5.3-codex") == "gpt-5.3-codex"
    assert managed_model_override_from_persisted_model("  claude-sonnet  ") == "claude-sonnet"


def test_replay_launch_policy_snapshot_normalizes_empty_model_to_none(tmp_path: Path) -> None:
    def terminal_surface_mode(*, harness_id: HarnessId) -> TerminalSurfaceMode:
        _ = harness_id
        return TerminalSurfaceMode.PTY_MEDIATED

    replayed = replay_launch_policy_snapshot(
        snapshot=LaunchPolicySnapshot(model="", harness="codex"),
        project_root=tmp_path,
        harness_registry=get_default_harness_registry(),
        skills_readonly=True,
        alias_catalog={},
        resolve_terminal_surface_mode=terminal_surface_mode,
    )

    assert replayed.model is None
    assert replayed.routing.model is None
    assert replayed.model_selection is None


def test_replay_launch_policy_snapshot_rejects_empty_harness(tmp_path: Path) -> None:
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
