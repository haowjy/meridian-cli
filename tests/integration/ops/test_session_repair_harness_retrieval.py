"""Session repair retrieval through tracked harness metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from meridian.lib.harness.claude import project_slug
from meridian.lib.ops.session_repair import (
    SessionRepairInput,
    repair_session_reference_sync,
)
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


def _write_claude_session(
    *,
    config_root: Path,
    project_root: Path,
    session_id: str,
) -> None:
    project_dir = config_root / "projects" / project_slug(project_root)
    project_dir.mkdir(parents=True)
    (project_dir / f"{session_id}.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"sessionId": session_id}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": "canonical transcript"}]
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("ref", ["c1", "p1"])
def test_session_repair_resolves_tracked_claude_session_from_canonical_root(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    ref: str,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    home = tmp_path / "home"
    recorded_config_root = tmp_path / "recorded-overlay"
    monkeypatch.setenv("HOME", home.as_posix())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", (tmp_path / "unrelated-overlay").as_posix())

    session_id = "claude-canonical-repair-session"
    _write_claude_session(
        config_root=home / ".claude",
        project_root=project_root,
        session_id=session_id,
    )
    session_store.start_session(
        runtime_root,
        harness="claude",
        harness_session_id=session_id,
        model="claude-opus",
        chat_id="c1",
        claude_config_dir=recorded_config_root.as_posix(),
        spawn_id="p1",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="claude-opus",
        agent="coder",
        harness="claude",
        prompt="hello",
        spawn_id="p1",
        harness_session_id=session_id,
        claude_config_dir=recorded_config_root.as_posix(),
        started_at="2026-04-11T00:00:00Z",
    )

    output = repair_session_reference_sync(
        SessionRepairInput(ref=ref, project_root=project_root.as_posix())
    )

    assert output.detected_harness_session_id == session_id
    assert output.source == "claude transcript"
    assert output.reason is None
