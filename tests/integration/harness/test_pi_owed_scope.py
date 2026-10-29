"""A Pi launch hands its spawn-watch extension core's owed parent scope."""

from collections.abc import Mapping
from pathlib import Path

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.pi import PI_OWED_PARENT_IDS_ENV, PiAdapter
from meridian.lib.harness.pi_runtime_resolver import PiLaunchRole, PiRuntimeResolution
from meridian.lib.launch.constants import BLOCKED_CHILD_ENV_VARS
from meridian.lib.launch.request import SessionRequest
from meridian.lib.ops.spawn.owed import discover_owed_scope
from meridian.lib.state.spawn.model import SpawnRecord, TerminalFacts
from meridian.lib.state.spawn.repository import record_to_stored_state
from tests.support.history import warm_history_index
from tests.support.launch_paths import launch_paths


def _seed_primary(runtime_root: Path, spawn_id: str, chat_id: str) -> None:
    record = SpawnRecord(
        id=spawn_id,
        chat_id=chat_id,
        kind="primary",
        harness="pi",
        status="succeeded",
        started_at="2026-05-01T00:00:00Z",
        terminal=TerminalFacts(
            exit_code=0,
            finished_at="2026-05-01T00:01:00Z",
            published_at="2026-05-01T00:01:00Z",
            origin="runner",
        ),
    )
    spawn_dir = runtime_root / "spawns" / spawn_id
    spawn_dir.mkdir(parents=True)
    (spawn_dir / "state.json").write_text(
        record_to_stored_state(record).model_dump_json() + "\n",
        encoding="utf-8",
    )


def _resolve_pi_runtime(*, env: Mapping[str, str], role: PiLaunchRole) -> PiRuntimeResolution:
    _ = env, role
    return PiRuntimeResolution(
        binary_path="/bound/bin/pi",
        runtime_kind="override",
        runtime_version="pi 1.1.0",
    )


def _prepare_prelaunch(
    monkeypatch: pytest.MonkeyPatch,
    runtime_root: Path,
    *,
    role: str,
    chat_id: str,
) -> dict[str, str]:
    monkeypatch.setattr("meridian.lib.harness.pi.resolve_pi_runtime", _resolve_pi_runtime)
    state = PiAdapter().prepare_prelaunch(
        launch=launch_paths(runtime_root, SpawnId("p-resumed")),
        session=SessionRequest(),
        child_cwd=runtime_root,
        child_env={
            "MERIDIAN_SESSION_ROLE": role,
            "MERIDIAN_CHAT_ID": chat_id,
            "PI_CODING_AGENT_DIR": str(runtime_root / "agent"),
            "PI_CODING_AGENT_SESSION_DIR": str(runtime_root / "sessions"),
        },
        resolved_harness_session_id="",
    )
    return state.env_overrides


def test_pi_primary_launch_carries_core_owed_parent_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    for spawn_id, chat_id in (
        ("p-old", "c1"),
        ("p-crashed", "c1"),
        ("p-resumed", "c1"),
        ("p-other-chat", "c2"),
    ):
        _seed_primary(runtime_root, spawn_id, chat_id)
    warm_history_index(runtime_root)

    overrides = _prepare_prelaunch(monkeypatch, runtime_root, role="primary", chat_id="c1")

    handed = tuple(sorted(overrides[PI_OWED_PARENT_IDS_ENV].split(",")))
    scope = discover_owed_scope(
        runtime_root=runtime_root,
        chat_id="c1",
        caller_spawn_id="p-resumed",
    )
    # A resumed primary must deliver what every earlier generation of its chat
    # launched, exactly as `spawn collect` decides it.
    assert handed == ("p-crashed", "p-old", "p-resumed")
    assert handed == tuple(sorted(scope.parent_ids))


def test_pi_spawned_launch_carries_no_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    _seed_primary(runtime_root, "p-resumed", "c1")

    overrides = _prepare_prelaunch(monkeypatch, runtime_root, role="spawn", chat_id="c1")

    # A nested session owns only its own direct children, so it must be handed no
    # wider scope and its descendants must not inherit one either.
    assert PI_OWED_PARENT_IDS_ENV not in overrides
    assert PI_OWED_PARENT_IDS_ENV in BLOCKED_CHILD_ENV_VARS
