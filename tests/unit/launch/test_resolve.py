from __future__ import annotations

from collections.abc import Callable

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
    ("resolver", "kwargs", "expected"),
    [
        pytest.param(
            resolve_pi_child_wave_timeout_seconds,
            {"explicit_timeout_seconds": None, "config_snapshot": None},
            300.0,
            id="child-default",
        ),
        pytest.param(
            resolve_pi_child_wave_timeout_seconds,
            {
                "explicit_timeout_seconds": 42.0,
                "config_snapshot": {"pi_child_wave_timeout_seconds": 10.0},
            },
            42.0,
            id="child-explicit",
        ),
        pytest.param(
            resolve_pi_child_wave_timeout_seconds,
            {
                "explicit_timeout_seconds": None,
                "config_snapshot": {"timeouts": {"pi_child_wave_timeout_seconds": 12.5}},
            },
            12.5,
            id="child-nested-config",
        ),
        pytest.param(
            resolve_pi_task_ping_interval_seconds,
            {
                "explicit_interval_seconds": 120.0,
                "config_snapshot": {"pi_task_ping_interval_seconds": 60.0},
            },
            120.0,
            id="ping-explicit",
        ),
        pytest.param(
            resolve_pi_task_ping_interval_seconds,
            {
                "explicit_interval_seconds": None,
                "config_snapshot": {"timeouts": {"pi_task_ping_interval_seconds": 3300.0}},
            },
            3300.0,
            id="ping-nested-config",
        ),
        pytest.param(
            resolve_pi_child_wave_timeout_seconds,
            {
                "explicit_timeout_seconds": None,
                "config_snapshot": MeridianConfig(
                    pi_child_wave_timeout_seconds=99.0,
                    pi_task_ping_interval_seconds=1800.0,
                ).model_dump(mode="json", exclude_none=True),
            },
            99.0,
            id="typed-child-config",
        ),
        pytest.param(
            resolve_pi_task_ping_interval_seconds,
            {
                "explicit_interval_seconds": None,
                "config_snapshot": MeridianConfig(
                    pi_child_wave_timeout_seconds=99.0,
                    pi_task_ping_interval_seconds=1800.0,
                ).model_dump(mode="json", exclude_none=True),
            },
            1800.0,
            id="typed-ping-config",
        ),
    ],
)
def test_resolve_pi_timeout(
    resolver: Callable[..., float],
    kwargs: dict[str, object],
    expected: float,
) -> None:
    assert resolver(**kwargs) == expected
