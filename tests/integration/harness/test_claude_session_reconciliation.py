"""Claude TUI trampoline session reconciliation tests."""

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.claude_sessions import project_slug, reconcile_tui_trampoline_session_id
from meridian.lib.state.artifact_store import InMemoryStore


@pytest.fixture(autouse=True)
def _clear_inherited_claude_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _write_jsonl(path: Path, *rows: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _project_dir(fake_home: Path, project_root: Path) -> Path:
    return fake_home / ".claude" / "projects" / project_slug(project_root)


def _history_row(
    *,
    project_root: Path,
    session_id: str,
    display: str,
    timestamp: int = 1_781_827_539_538,
) -> dict[str, object]:
    return {
        "display": display,
        "project": project_root.as_posix(),
        "sessionId": session_id,
        "timestamp": timestamp,
    }


def _write_history(fake_home: Path, *rows: dict[str, object]) -> None:
    _write_jsonl(fake_home / ".claude" / "history.jsonl", *rows)


def _write_transcript(
    project_dir: Path,
    *,
    session_id: str,
    prompt: str | None = None,
    timestamp: int = 1_781_827_539_538,
) -> Path:
    rows: list[dict[str, object]] = [{"type": "agent-setting", "sessionId": session_id}]
    if prompt is not None:
        rows.append(
            {
                "type": "user",
                "message": {"role": "user", "content": prompt},
                "timestamp": timestamp,
                "sessionId": session_id,
            }
        )
    transcript = project_dir / f"{session_id}.jsonl"
    _write_jsonl(transcript, *rows)
    return transcript


def test_claude_reconciliation_keeps_recorded_id_when_transcript_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    recorded_session_id = str(uuid4())
    real_session_id = str(uuid4())
    project_dir = _project_dir(fake_home, project_root)
    _write_transcript(project_dir, session_id=recorded_session_id)
    _write_history(
        fake_home,
        _history_row(
            project_root=project_root,
            session_id=recorded_session_id,
            display="/tui fullscreen",
            timestamp=1_781_827_479_996,
        ),
        _history_row(project_root=project_root, session_id=real_session_id, display="real prompt"),
    )

    assert (
        reconcile_tui_trampoline_session_id(
            project_root=project_root,
            recorded_session_id=recorded_session_id,
            started_at_epoch=None,
        )
        == recorded_session_id
    )


def test_claude_reconciliation_replaces_tui_trampoline_with_successor_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    recorded_session_id = str(uuid4())
    real_session_id = str(uuid4())
    real_transcript = _write_transcript(
        _project_dir(fake_home, project_root),
        session_id=real_session_id,
        prompt="real prompt",
    )
    now = time.time()
    os.utime(real_transcript, (now, now))
    _write_history(
        fake_home,
        _history_row(
            project_root=project_root,
            session_id=recorded_session_id,
            display="/tui fullscreen",
            timestamp=1_781_827_479_996,
        ),
        _history_row(project_root=project_root, session_id=real_session_id, display="real prompt"),
    )

    adapter = ClaudeAdapter()
    assert (
        adapter.observe_session_id(
            artifacts=InMemoryStore(),
            current_session_id=recorded_session_id,
            project_root=project_root,
            started_at_epoch=now - 1,
        )
        == real_session_id
    )


def test_claude_reconciliation_preserves_recorded_id_without_trampoline_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    recorded_session_id = str(uuid4())
    real_session_id = str(uuid4())
    _write_transcript(_project_dir(fake_home, project_root), session_id=real_session_id)
    _write_history(
        fake_home,
        _history_row(
            project_root=project_root,
            session_id=real_session_id,
            display="unrelated prompt",
        ),
    )

    assert (
        ClaudeAdapter().observe_session_id(
            artifacts=InMemoryStore(),
            current_session_id=recorded_session_id,
            project_root=project_root,
            started_at_epoch=None,
        )
        == recorded_session_id
    )


def test_claude_reconciliation_preserves_recorded_id_for_existing_same_project_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    recorded_session_id = str(uuid4())
    unrelated_session_id = str(uuid4())
    unrelated_transcript = _write_transcript(
        _project_dir(fake_home, project_root),
        session_id=unrelated_session_id,
        prompt="same prompt",
        timestamp=1_781_827_469_538,
    )
    now = time.time()
    os.utime(unrelated_transcript, (now, now))
    _write_history(
        fake_home,
        _history_row(
            project_root=project_root,
            session_id=recorded_session_id,
            display="/tui fullscreen",
            timestamp=1_781_827_479_996,
        ),
        _history_row(
            project_root=project_root,
            session_id=unrelated_session_id,
            display="same prompt",
        ),
    )

    assert (
        ClaudeAdapter().observe_session_id(
            artifacts=InMemoryStore(),
            current_session_id=recorded_session_id,
            project_root=project_root,
            started_at_epoch=now - 1,
        )
        == recorded_session_id
    )
