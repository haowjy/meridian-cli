"""Public-contract parity tests for launch projection behavior."""

from __future__ import annotations

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import Allow, build_permission_policy


@pytest.mark.parametrize(
    ("approval_mode", "expected"),
    [
        ("default", None),
        ("auto", "on-request"),
        ("confirm", "on-request"),
        ("yolo", "never"),
    ],
)
def test_codex_approval_policy_mapping_contract(approval_mode: str, expected: str | None) -> None:
    policy = build_permission_policy(
        sandbox=None,
        network=Allow(),
        approval=approval_mode,
    )
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CODEX,
        permission_policy=policy,
    )

    assert compile_codex_permissions(spec)[0].approval_policy == expected
