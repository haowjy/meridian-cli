from __future__ import annotations

from meridian.lib.core.lifecycle import _terminal_telemetry_payload
from meridian.lib.state.spawn.model import SpawnRecord


def _spawn_record(**overrides: object) -> SpawnRecord:
    defaults: dict[str, object] = {
        "id": "spawn-1",
        "chat_id": "chat-1",
        "parent_id": None,
        "model": "gpt-5.4",
        "agent": "coder",
        "agent_path": None,
        "skills": (),
        "skill_paths": (),
        "harness": "codex",
        "kind": "child",
        "desc": "desc",
        "work_id": "work-1",
        "goal": None,
        "harness_session_id": None,
        "execution_cwd": None,
        "claude_config_dir": None,
        "launch_mode": "background",
        "worker_pid": None,
        "runner_pid": None,
        "status": "succeeded",
        "prompt": "prompt",
        "started_at": "2026-05-01T00:00:00Z",
        "exited_at": None,
        "process_exit_code": None,
        "finished_at": "2026-05-01T00:00:05Z",
        "exit_code": 0,
        "duration_secs": 5.0,
        "total_cost_usd": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "reasoning_tokens": None,
        "cost_is_estimate": False,
        "error": None,
        "terminal_origin": "runner",
        "process_scopes": None,
    }
    defaults.update(overrides)
    return SpawnRecord(**defaults)


def test_terminal_telemetry_payload_includes_usage_fields_when_present() -> None:
    payload = _terminal_telemetry_payload(
        _spawn_record(
            total_cost_usd=1.25,
            input_tokens=100,
            output_tokens=200,
            cache_read_input_tokens=30,
            cache_creation_input_tokens=40,
            reasoning_tokens=50,
            cost_is_estimate=True,
        )
    )

    assert payload["total_cost_usd"] == 1.25
    assert payload["input_tokens"] == 100
    assert payload["output_tokens"] == 200
    assert payload["cache_read_input_tokens"] == 30
    assert payload["cache_creation_input_tokens"] == 40
    assert payload["reasoning_tokens"] == 50
    assert payload["cost_is_estimate"] is True


def test_terminal_telemetry_payload_omits_usage_fields_when_absent() -> None:
    payload = _terminal_telemetry_payload(_spawn_record())

    assert "total_cost_usd" not in payload
    assert "input_tokens" not in payload
    assert "output_tokens" not in payload
    assert "cache_read_input_tokens" not in payload
    assert "cache_creation_input_tokens" not in payload
    assert "reasoning_tokens" not in payload
    assert "cost_is_estimate" not in payload
