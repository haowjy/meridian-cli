from __future__ import annotations

from pathlib import Path

from meridian.lib.state.spawn_signals import (
    consume_spawn_signal,
    spawn_signal_path,
    write_spawn_signal,
)


def test_spawn_signal_write_consume_round_trip(tmp_path: Path) -> None:
    write_spawn_signal(tmp_path, "p1", "done")
    write_spawn_signal(tmp_path, "p1", "rearm")

    assert spawn_signal_path(tmp_path, "p1", "done").is_file()
    assert consume_spawn_signal(tmp_path, "p1", "done") is True
    assert consume_spawn_signal(tmp_path, "p1", "done") is False
    assert consume_spawn_signal(tmp_path, "p1", "rearm") is True
