"""On-disk contract: the ledgers older builds wrote stay readable, as launch 1."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.harness.pi_private_state import pi_coordination_dir, read_pi_consumption_state
from meridian.lib.state.paths import observed_spawns_path


def test_old_ledgers_read_as_launch_one(tmp_path: Path) -> None:
    observed = observed_spawns_path(tmp_path, "p1")
    observed.parent.mkdir(parents=True)
    observed.write_text(
        json.dumps({"v": 1, "spawn_id": "p1", "observed_spawn_ids": ["p2"]}), encoding="utf-8"
    )
    coordination = pi_coordination_dir(tmp_path, "p1")
    coordination.mkdir(parents=True)
    (coordination / "delivery-receipts.json").write_text(
        json.dumps({"v": 1, "spawn_id": "p1", "messages": {"m1": ["p3", "bash-9"]}}),
        encoding="utf-8",
    )

    state = read_pi_consumption_state(tmp_path, "p1")

    assert {"p2:1", "p3:1", "p3", "bash-9"} <= state.consumed_ids
    assert "p2:2" not in state.consumed_ids
    assert state.receipt_message_ids == {"m1"}
