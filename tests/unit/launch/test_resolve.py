from __future__ import annotations

import pytest

from meridian.lib.config.models import MeridianConfig
from meridian.lib.launch.resolve import (
    AgentLaunchInput,
    parse_duration_seconds,
    resolve_agent_launch_input,
    resolve_pi_child_wave_timeout_seconds,
    resolve_pi_task_ping_interval_seconds,
)


def test_resolve_agent_launch_input_tri_state() -> None:
    assert resolve_agent_launch_input(None) == AgentLaunchInput()
    assert resolve_agent_launch_input("") == AgentLaunchInput(agent_opt_out=True)
    assert resolve_agent_launch_input("  ") == AgentLaunchInput(agent_opt_out=True)
    assert resolve_agent_launch_input(" coder ") == AgentLaunchInput(agent="coder")


def test_parse_duration_seconds() -> None:
    assert parse_duration_seconds("90m") == 5400.0
    assert parse_duration_seconds("1h") == 3600.0
    assert parse_duration_seconds("12500") == 12500.0
    assert parse_duration_seconds("") is None


@pytest.mark.parametrize(
    ("explicit", "snapshot", "expected"),
    [
        (None, None, 300.0),
        (42.0, {"pi_child_wave_timeout_seconds": 10.0}, 42.0),
        (None, {"timeouts": {"pi_child_wave_timeout_seconds": 12.5}}, 12.5),
    ],
    ids=["default", "explicit", "nested-config"],
)
def test_resolve_pi_child_wave_timeout(
    explicit: float | None,
    snapshot: dict[str, object] | None,
    expected: float,
) -> None:
    assert (
        resolve_pi_child_wave_timeout_seconds(
            explicit_timeout_seconds=explicit,
            config_snapshot=snapshot,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("explicit", "snapshot", "expected"),
    [
        (120.0, {"pi_task_ping_interval_seconds": 60.0}, 120.0),
        (None, {"timeouts": {"pi_task_ping_interval_seconds": 3300.0}}, 3300.0),
    ],
    ids=["explicit", "nested-config"],
)
def test_resolve_pi_task_ping_interval(
    explicit: float | None,
    snapshot: dict[str, object] | None,
    expected: float,
) -> None:
    assert (
        resolve_pi_task_ping_interval_seconds(
            explicit_interval_seconds=explicit,
            config_snapshot=snapshot,
        )
        == expected
    )


def test_pi_timeouts_use_typed_meridian_config_fields() -> None:
    snapshot = MeridianConfig(
        pi_child_wave_timeout_seconds=99.0,
        pi_task_ping_interval_seconds=1800.0,
    ).model_dump(mode="json", exclude_none=True)

    assert (
        resolve_pi_child_wave_timeout_seconds(
            explicit_timeout_seconds=None,
            config_snapshot=snapshot,
        )
        == 99.0
    )
    assert (
        resolve_pi_task_ping_interval_seconds(
            explicit_interval_seconds=None,
            config_snapshot=snapshot,
        )
        == 1800.0
    )
