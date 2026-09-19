"""Native-history last-executed-model readers across harnesses."""

from __future__ import annotations

import json
from pathlib import Path

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
    context = NativeModelReadContext(launch_env={"OPENCODE_DB": str(db)})

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
        project_root=str(project_root), claude_config_dir=str(config_root)
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
    context = NativeModelReadContext(launch_env={"CODEX_HOME": str(codex_home)})

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
    context = NativeModelReadContext(pi_session_dir=str(session_dir))

    assert read_last_executed_model("pi", session_id, context=context) == "deepseek-v4-pro"
    assert read_last_executed_model("pi", "missing-session", context=context) is None


def test_unknown_harness_and_missing_store_return_none(tmp_path: Path) -> None:
    empty = NativeModelReadContext()

    assert read_last_executed_model("cursor", "ses-1", context=empty) is None
    assert read_last_executed_model("", "", context=empty) is None
    assert read_last_executed_model("claude", "ses-1", context=empty) is None
    assert (
        read_last_executed_model(
            "codex",
            "missing",
            context=NativeModelReadContext(launch_env={"CODEX_HOME": str(tmp_path / "nope")}),
        )
        is None
    )
    assert (
        read_last_executed_model(
            "pi",
            "missing",
            context=NativeModelReadContext(pi_session_dir=str(tmp_path / "nope")),
        )
        is None
    )
