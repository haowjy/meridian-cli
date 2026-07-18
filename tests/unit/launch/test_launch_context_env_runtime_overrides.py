# qa-validated: test-suite-redesign
"""Runtime override snapshot precedence and bind-time spawn identity."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import LaunchCompositionSurface
from tests.support.launch import stub_bundle_request_and_resolve
from tests.unit.launch.context_env_helpers import (
    build_launch_runtime,
    build_spawn_request,
    write_minimal_mars_config,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_build_launch_context_uses_runtime_override_snapshot_not_live_env(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    runtime = build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    ).model_copy(
        update={
            "runtime_override_snapshot": RuntimeOverrides(approval="confirm").model_dump(
                mode="json",
                exclude_none=True,
            )
        }
    )
    monkeypatch.setenv("MERIDIAN_APPROVAL", "yolo")

    runtime_ctx = build_launch_context(
        spawn_id="p-snapshot",
        request=build_spawn_request(),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.resolved_request.execution_policy.approval == "confirm"
    assert runtime_ctx.binding.environment.final_env["MERIDIAN_APPROVAL"] == "confirm"


def test_build_launch_context_explicit_empty_snapshot_blocks_live_policy_env_leak(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    runtime = build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    ).model_copy(update={"runtime_override_snapshot": {}})
    monkeypatch.setenv("MERIDIAN_APPROVAL", "yolo")

    runtime_ctx = build_launch_context(
        spawn_id="p-empty-snapshot",
        request=build_spawn_request(),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.resolved_request.execution_policy.approval is None
    assert "MERIDIAN_APPROVAL" not in runtime_ctx.binding.environment.final_env


@pytest.mark.parametrize(
    ("parent_depth", "expected_depth"),
    [
        pytest.param(None, "0", id="clean-shell-primary-root"),
        pytest.param("2", "2", id="primary-launched-from-existing-depth"),
    ],
)
def test_build_launch_context_primary_preserves_runtime_depth(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    parent_depth: str | None,
    expected_depth: str,
) -> None:
    write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.delenv("_MERIDIAN_DEPTH", raising=False)
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    if parent_depth is not None:
        monkeypatch.setenv("_MERIDIAN_DEPTH", parent_depth)
    runtime = build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary",
        request=build_spawn_request(),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.binding.environment.bind_env_overrides["_MERIDIAN_DEPTH"] == expected_depth
    assert runtime_ctx.binding.environment.bind_env_overrides["MERIDIAN_SPAWN_ID"] == "p-primary"
    assert "_MERIDIAN_PARENT_SPAWN_ID" not in runtime_ctx.binding.environment.bind_env_overrides


def test_build_launch_context_emits_child_spawn_id(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-parent")
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    runtime_ctx = build_launch_context(
        spawn_id="p-child",
        request=build_spawn_request(),
        runtime=build_launch_runtime(tmp_path=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    bind_env = runtime_ctx.binding.environment.bind_env_overrides
    assert bind_env["MERIDIAN_SPAWN_ID"] == "p-child"
    assert bind_env["_MERIDIAN_PARENT_SPAWN_ID"] == "p-parent"
