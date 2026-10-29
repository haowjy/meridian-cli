"""Caller-boundary parsing and projection, independent of any launch."""

from __future__ import annotations

import json

import pytest

from meridian.lib.core.caller_boundary import (
    CALLER_BOUNDARY_ENV,
    CallerBoundary,
    CallerBoundaryCheck,
)
from meridian.lib.safety.policy import (
    EffectivePolicy,
    PermissionPolicy,
    SandboxMode,
    network_policy_to_wire,
    parse_network_policy,
)


def test_unsandboxed_caller_has_no_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        CALLER_BOUNDARY_ENV,
        json.dumps(
            {
                "pid_namespace": None,
                "sandbox": "default",
                # A newer axis and a missing spawn axis are irrelevant when unfloored.
                "future_axis": "ignored",
            }
        ),
    )

    check = CallerBoundaryCheck.from_environment(
        dry_run=True,
        passthrough_args=("--dangerously-skip-permissions",),
    )

    assert check.caller is None


@pytest.mark.parametrize("legacy_network", [None, "allow"], ids=["missing", "allow"])
def test_floored_legacy_caller_preserves_optional_network_floor(
    monkeypatch: pytest.MonkeyPatch,
    legacy_network: str | None,
) -> None:
    payload: dict[str, object] = {
        "pid_namespace": None,
        "sandbox": "workspace-write",
        "spawn": True,
    }
    if legacy_network is not None:
        payload["network"] = legacy_network
    monkeypatch.setenv(
        CALLER_BOUNDARY_ENV,
        json.dumps(payload),
    )

    check = CallerBoundaryCheck.from_environment(dry_run=True)

    assert check.caller is not None
    assert check.caller.network == parse_network_policy(legacy_network)


@pytest.mark.parametrize(
    ("sandbox", "effective_network", "expected_floor"),
    [
        ("workspace-write", "allow", "allow"),
        ("workspace-write", "deny", "deny"),
        ("danger-full-access", "allow", None),
    ],
)
def test_caller_boundary_records_effective_network_for_every_sandboxed_caller(
    sandbox: SandboxMode,
    effective_network: str,
    expected_floor: str | None,
) -> None:
    parsed_effective = parse_network_policy(effective_network)
    assert parsed_effective is not None
    boundary = CallerBoundary.from_policy(
        PermissionPolicy(sandbox=sandbox),
        effective=EffectivePolicy(sandbox=sandbox, network=parsed_effective),
        pid_namespace=None,
    )

    parsed_floor = parse_network_policy(expected_floor)
    assert boundary.network == parsed_floor
    assert json.loads(boundary.to_env_value()).get("network") == (
        network_policy_to_wire(parsed_floor) if parsed_floor is not None else None
    )
