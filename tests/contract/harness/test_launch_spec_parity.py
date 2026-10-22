"""Public-contract parity tests for launch projection behavior."""

from __future__ import annotations

import pytest

from meridian.lib.harness.projections.permissions_codex import map_codex_approval_policy
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch


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
    assert map_codex_approval_policy(approval_mode) == expected


def test_codex_approval_policy_rejects_unsupported_modes_fail_closed() -> None:
    with pytest.raises(HarnessCapabilityMismatch, match="approval mode 'legacy-mode'"):
        map_codex_approval_policy("legacy-mode")
