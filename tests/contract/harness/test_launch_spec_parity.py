"""Public-contract parity tests for launch projection behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.connections.base import ObserverEndpoint
from meridian.lib.harness.passthrough.codex import CodexPassthrough
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


@pytest.mark.parametrize(
    ("prompt", "expected_tail"),
    [("continue here", ("continue here",)), (" \t", ())],
)
def test_codex_managed_attach_does_not_override_remote_permissions(
    tmp_path: Path,
    prompt: str,
    expected_tail: tuple[str, ...],
) -> None:
    connection: Any = type(
        "FakeConnection",
        (),
        {
            "observer_endpoint": ObserverEndpoint(
                transport="ws",
                url="ws://127.0.0.1:43123",
                host="127.0.0.1",
                port=43123,
            ),
        },
    )()
    spec = ResolvedLaunchSpec(
        prompt="",
        user_turn_content=prompt,
        projected_roots=(tmp_path / "sibling",),
    )

    command = CodexPassthrough().build_tui_command(connection, spec)("thread-123")

    assert command == (
        "codex",
        "resume",
        "thread-123",
        "--remote",
        "ws://127.0.0.1:43123",
        *expected_tail,
    )
