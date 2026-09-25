"""Native-history last-executed-model readers across harnesses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.harness.claude_sessions import project_slug
from meridian.lib.harness.model_observation import (
    NativeModelReadContext,
    read_last_executed_model,
)
from tests.support.opencode_db import write_opencode_v2_db_session


def test_opencode_reads_last_model(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    write_opencode_v2_db_session(
        db_path=db,
        session_id="ses-1",
        model={"id": "deepseek-flash", "providerID": "deepseek"},
    )
    context = NativeModelReadContext(native_store=str(db))

    assert (
        read_last_executed_model("opencode", "ses-1", context=context)
        == "deepseek/deepseek-flash"
    )
    assert read_last_executed_model("opencode", "ses-missing", context=context) is None


def test_claude_reads_last_assistant_model(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config_root = tmp_path / "claude-config"
    project_dir = config_root / "projects" / project_slug(project_root)
    project_dir.mkdir(parents=True)
    (project_dir / "ses-1.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "model": "claude-opus-5"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "model": "claude-fable-5"},
                    }
                ),
                json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    context = NativeModelReadContext(
        project_root=str(project_root), native_store=str(project_dir)
    )

    assert read_last_executed_model("claude", "ses-1", context=context) == "claude-fable-5"
    assert read_last_executed_model("claude", "ses-missing", context=context) is None


def test_codex_reads_last_model(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    sessions_root = codex_home / "sessions"
    sessions_root.mkdir(parents=True)
    session_id = "019fb673-854a-7b63-97b8-360bece12926"
    rollout = sessions_root / f"rollout-2026-07-30T23-34-11-{session_id}.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-luna"}}),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant", "content": []},
                    }
                ),
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    context = NativeModelReadContext(native_store=str(sessions_root))

    assert read_last_executed_model("codex", session_id, context=context) == "gpt-5.6-sol"
    assert (
        read_last_executed_model(
            "codex", "019fb673-854a-7b63-97b8-360bece19999", context=context
        )
        is None
    )


def test_pi_reads_last_model_change(tmp_path: Path) -> None:
    session_dir = tmp_path / "pi-sessions"
    session_dir.mkdir()
    session_id = "01a0aba2-1687-748e-95da-26d7c0906dfd"
    (session_dir / f"2026-09-16T19-12-01-672Z_{session_id}.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "version": 3, "id": session_id, "cwd": "/x"}),
                json.dumps(
                    {
                        "type": "model_change",
                        "id": "a",
                        "parentId": None,
                        "provider": "deepseek",
                        "modelId": "deepseek-v4-pro",
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "id": "b",
                        "parentId": "a",
                        "message": {"role": "user", "content": "hi"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    context = NativeModelReadContext(native_store=str(session_dir))

    assert read_last_executed_model("pi", session_id, context=context) == "deepseek-v4-pro"
    assert read_last_executed_model("pi", "missing-session", context=context) is None


def test_unknown_harness_returns_none() -> None:
    empty = NativeModelReadContext()

    assert read_last_executed_model("cursor", "ses-1", context=empty) is None
    assert read_last_executed_model("", "", context=empty) is None


def test_recorded_model_read_does_not_fall_back_to_ambient(
    tmp_path: Path, monkeypatch,
) -> None:
    sid = "12345678-1234-4234-8234-123456789abc"
    root = tmp_path / "repo"
    for harness, env_key, suffix, filename, event in (
        ("claude", "CLAUDE_CONFIG_DIR", Path("projects") / project_slug(root),
         f"{sid}.jsonl", {"type": "assistant", "message": {"model": "wrong"}}),
        ("codex", "CODEX_HOME", Path("sessions"),
         f"rollout-2026-01-01T00-00-00-{sid}.jsonl",
         {"type": "turn_context", "payload": {"model": "wrong"}}),
        ("pi", "PI_CODING_AGENT_DIR", Path("sessions"),
         f"2026-01-01T00-00-00_{sid}.jsonl", {"type": "model_change", "modelId": "wrong"}),
    ):
        home = tmp_path / harness
        decoy = home / suffix / filename
        decoy.parent.mkdir(parents=True)
        decoy.write_text(json.dumps(event) + "\n")
        monkeypatch.setenv(env_key, str(home))
        assert read_last_executed_model(
            harness, sid,
            context=NativeModelReadContext(
                project_root=str(root), native_store=str(tmp_path / "missing"),
            ),
        ) is None


@pytest.mark.parametrize("header", ["{torn header", '{"type":"session","id":"different-id"}'])
def test_unreadable_pi_header_model_observation_is_best_effort(
    tmp_path: Path, header: str,
) -> None:
    sid = "12345678-1234-4234-8234-123456789abc"
    (tmp_path / f"2026-01-01T00-00-00_{sid}.jsonl").write_text(header + "\n")
    assert read_last_executed_model(
        "pi", sid, context=NativeModelReadContext(native_store=str(tmp_path)),
    ) is None
