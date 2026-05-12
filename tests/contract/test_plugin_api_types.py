from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.plugin_api import Hook, HookContext, HookResult


def test_hook_construction_defaults_and_frozen_behavior() -> None:
    hook = Hook(
        name="git-autosync",
        event="spawn.finalized",
        source="user",
        builtin="git-autosync",
    )

    assert hook.name == "git-autosync"
    assert hook.event == "spawn.finalized"
    assert hook.source == "user"
    assert hook.builtin == "git-autosync"
    assert hook.enabled is True
    assert hook.priority == 0
    assert hook.require_serial is False
    assert hook.exclude == ()
    assert hook.remote is None

    with pytest.raises(dataclasses.FrozenInstanceError):
        hook.name = "cannot-mutate"  # type: ignore[misc]


def test_hook_context_to_env_and_json() -> None:
    event_id = uuid4()
    context = HookContext(
        event_name="spawn.finalized",
        event_id=event_id,
        timestamp="2026-04-20T12:00:00+00:00",
        project_root="/repo",
        runtime_root="/repo/.meridian",
        spawn_id="p123",
        spawn_status="success",
        spawn_agent="coder",
        spawn_model="gpt-5.3-codex",
        spawn_duration_secs=3.5,
        spawn_cost_usd=0.02,
        work_id="ref-ame-009",
        work_dir="/repo/.meridian/work/ref-ame-009",
    )

    env = context.to_env()
    payload = json.loads(context.to_json())

    assert env["MERIDIAN_HOOK_EVENT"] == "spawn.finalized"
    assert env["MERIDIAN_HOOK_EVENT_ID"] == str(event_id)
    assert env["MERIDIAN_HOOK_SCHEMA_VERSION"] == "1"
    assert env["MERIDIAN_SPAWN_ID"] == "p123"
    assert env["MERIDIAN_ACTIVE_WORK_ID"] == "ref-ame-009"

    assert payload["schema_version"] == 1
    assert payload["event_name"] == "spawn.finalized"
    assert payload["event_id"] == str(event_id)
    assert payload["spawn"]["id"] == "p123"
    assert payload["spawn"]["status"] == "success"
    assert payload["work"]["id"] == "ref-ame-009"


def test_hook_result_construction_and_asdict_shape() -> None:
    result = HookResult(
        hook_name="git-autosync",
        event="spawn.finalized",
        outcome="success",
        success=True,
        duration_ms=25,
        stdout="ok",
    )

    assert dataclasses.asdict(result) == {
        "hook_name": "git-autosync",
        "event": "spawn.finalized",
        "outcome": "success",
        "success": True,
        "skipped": False,
        "skip_reason": None,
        "error": None,
        "exit_code": None,
        "duration_ms": 25,
        "stdout": "ok",
        "stderr": None,
    }


def test_plugin_hook_context_from_roots_normalizes_paths() -> None:
    context = HookContext.from_roots(
        project_root=Path("/repo"),
        runtime_root=Path("/repo/.meridian"),
        event_name="work.done",
        event_id=uuid4(),
        timestamp="2026-04-20T12:00:00+00:00",
    )

    assert context.project_root == "/repo"
    assert context.runtime_root == "/repo/.meridian"
