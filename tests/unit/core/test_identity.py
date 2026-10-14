"""Ambient identity must not follow a newer run when an old runner calls the CLI."""

from meridian.lib.core.identity import current_run_id, current_spawn_id
from meridian.lib.core.refs import parse_ref
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.types import SpawnId


def test_old_launcher_fallback_and_new_run_identity():
    assert current_run_id({}) is None
    assert current_spawn_id({}) is None
    assert current_run_id({"MERIDIAN_SPAWN_ID": " p2 "}) == "p2"
    env = {"MERIDIAN_SPAWN_ID": "p2", "MERIDIAN_RUN_ID": "p2.3"}
    assert current_spawn_id(env) == "p2"
    assert current_run_id(env) == "p2.3"
    assert current_run_id({**env, "MERIDIAN_RUN_ID": " "}) == "p2"


def test_child_environment_carries_both_identities():
    parent = ResolvedContext(spawn_id=SpawnId("p2"), run_id=parse_ref("p2.3", row_key=True))
    child = parent.child_env_overrides(child_spawn_id="p5.2")
    assert child["MERIDIAN_SPAWN_ID"] == "p5"
    assert child["MERIDIAN_RUN_ID"] == "p5.2"
    assert child["_MERIDIAN_PARENT_SPAWN_ID"] == "p2"
    assert parent.child_env_overrides()["MERIDIAN_RUN_ID"] == "p2.3"
