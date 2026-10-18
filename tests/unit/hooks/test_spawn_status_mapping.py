from typing import get_args

from meridian.lib.core.domain import TerminalSpawnStatus
from meridian.lib.hooks.dispatch import (
    _normalize_spawn_status,  # pyright: ignore[reportPrivateUsage]
)


def test_every_terminal_spawn_status_reaches_when_status() -> None:
    mapped = {
        status: _normalize_spawn_status(status)
        for status in get_args(TerminalSpawnStatus.__value__)
    }

    assert mapped == {
        "succeeded": "success",
        "failed": "failure",
        "cancelled": "cancelled",
        "timed_out": "timeout",
    }
