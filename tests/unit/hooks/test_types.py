from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import get_args
from uuid import uuid4

from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.core.lifecycle import generate_lifecycle_event_id
from meridian.lib.hooks.types import HookContext, HookEventName, HookResult
from meridian.lib.ops.runtime import RuntimeAuthoritySnapshot


def test_hook_event_name_literals_exclude_session_idle() -> None:
    assert get_args(HookEventName) == (
        "spawn.created",
        "spawn.running",
        "spawn.start",
        "spawn.finalized",
        "work.start",
        "work.started",
        "work.done",
    )


def test_hook_context_to_env_includes_required_and_present_optional_fields() -> None:
    context = HookContext(
        event_name="spawn.finalized",
        event_id=uuid4(),
        timestamp="2026-04-19T12:00:00+00:00",
        project_root="/repo",
        runtime_root="/repo/.meridian",
        spawn_id="p123",
        spawn_status="cancelled",
        spawn_agent="reviewer",
        spawn_model="gpt-5.3-codex",
        spawn_duration_secs=1.5,
        spawn_cost_usd=0.01,
        spawn_error="cancelled by user",
        work_id="hook-system-design",
        work_dir="/repo/.meridian/work/hook-system-design",
    )

    env = context.to_env()

    assert env["MERIDIAN_HOOK_EVENT"] == "spawn.finalized"
    assert env["MERIDIAN_HOOK_EVENT_ID"] == str(context.event_id)
    assert env["MERIDIAN_HOOK_SCHEMA_VERSION"] == "1"
    assert env["MERIDIAN_PROJECT_DIR"] == "/repo"
    assert env["MERIDIAN_RUNTIME_DIR"] == "/repo/.meridian"
    assert env["MERIDIAN_SPAWN_ID"] == "p123"
    assert env["MERIDIAN_SPAWN_STATUS"] == "cancelled"
    assert env["MERIDIAN_ACTIVE_WORK_ID"] == "hook-system-design"
    assert env["MERIDIAN_ACTIVE_WORK_DIR"] == "/repo/.meridian/work/hook-system-design"


def test_hook_context_to_json_serializes_spawn_and_work_payloads() -> None:
    event_id = uuid4()
    context = HookContext(
        event_name="work.started",
        event_id=event_id,
        timestamp="2026-04-19T12:00:00+00:00",
        project_root="/repo",
        runtime_root="/repo/.meridian",
        work_id="hook-system-design",
        work_dir="/repo/.meridian/work/hook-system-design",
    )

    payload = json.loads(context.to_json())

    assert payload["schema_version"] == 1
    assert payload["event_name"] == "work.started"
    assert payload["event_id"] == str(event_id)
    assert payload["work"] == {
        "id": "hook-system-design",
        "dir": "/repo/.meridian/work/hook-system-design",
    }
    assert payload["spawn"] is None


def test_hook_context_from_authority_uses_consistent_root_values() -> None:
    project_root = Path("/repo")
    authority = RuntimeAuthoritySnapshot(
        execution_cwd=project_root,
        project_root=project_root,
        project_root_source="explicit",
        project_config_paths=ProjectConfigPaths(
            project_root=project_root,
            execution_cwd=project_root,
        ),
        project_state_dir=project_root / ".meridian",
        user_home=Path("/home/test/.meridian"),
        runtime_root=Path("/runtime"),
        runtime_root_source="env",
    )

    context = HookContext.from_authority(
        authority=authority,
        event_name="spawn.created",
        event_id=uuid4(),
        timestamp="2026-04-19T12:00:00+00:00",
    )

    assert context.project_root == "/repo"
    assert context.runtime_root == "/runtime"


def test_hook_result_is_dataclass_serializable() -> None:
    result = HookResult(
        hook_name="notify",
        event="spawn.created",
        outcome="success",
        success=True,
        duration_ms=12,
        stdout="ok",
    )

    data = dataclasses.asdict(result)

    assert data == {
        "hook_name": "notify",
        "event": "spawn.created",
        "outcome": "success",
        "success": True,
        "skipped": False,
        "skip_reason": None,
        "error": None,
        "exit_code": None,
        "duration_ms": 12,
        "stdout": "ok",
        "stderr": None,
    }


def test_hook_event_ids_are_stable_for_non_spawn_events() -> None:
    event_id = generate_lifecycle_event_id("work-123", "work.done", 0)

    assert event_id == generate_lifecycle_event_id("work-123", "work.done", 0)
    assert event_id != generate_lifecycle_event_id("work-123", "work.done", 1)
