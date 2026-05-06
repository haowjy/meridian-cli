from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian.cli import primary_launch


def _resolved_reference(**overrides: object) -> object:
    payload: dict[str, object] = {
        "harness_session_id": "session-1",
        "source_chat_id": "c1",
        "harness": "claude",
        "source_model": "gpt-5.4",
        "source_agent": "coder",
        "source_work_id": None,
        "source_execution_cwd": "/tmp/source-cwd",
        "source_claude_config_dir": "/tmp/source-claude-config",
        "tracked": True,
        "warning": None,
    }
    payload.update(overrides)
    return type("Resolved", (), payload)()


def test_run_primary_launch_rejects_continue_cross_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_resolve_session_reference(_project_root: object, _ref: str) -> object:
        return _resolved_reference(source_execution_cwd=None)

    monkeypatch.setattr(
        primary_launch,
        "resolve_session_reference",
        _fake_resolve_session_reference,
    )
    with pytest.raises(ValueError, match="Cannot continue across harnesses"):
        primary_launch.run_primary_launch(
            continue_ref="session-1",
            fork_ref=None,
            model="",
            harness="codex",
            agent=None,
            work="",
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=True,
            passthrough=(),
        )


def test_resolve_session_target_threads_source_claude_config_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        primary_launch,
        "resolve_session_reference",
        lambda _project_root, _ref: _resolved_reference(
            source_claude_config_dir="/tmp/original-claude-config"
        ),
    )

    resolved = primary_launch.resolve_session_target(
        project_root=tmp_path,
        continue_ref="session-1",
    )

    assert resolved.source_claude_config_dir == "/tmp/original-claude-config"


def test_run_primary_launch_resume_threads_source_metadata_into_session_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        primary_launch,
        "resolve_session_reference",
        lambda _project_root, _ref: _resolved_reference(
            source_execution_cwd="/tmp/resume-cwd",
            source_claude_config_dir="/tmp/resume-claude-config",
        ),
    )

    captured: dict[str, object] = {}

    def _fake_launch_primary(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(
            exit_code=0,
            command=("claude", "--resume", "session-1"),
            continue_ref="session-1",
            warning=None,
        )

    monkeypatch.setattr(primary_launch, "launch_primary", _fake_launch_primary)

    primary_launch.run_primary_launch(
        project_root=tmp_path,
        continue_ref="session-1",
        fork_ref=None,
        model="",
        harness=None,
        agent=None,
        work="",
        yolo=False,
        approval=None,
        autocompact=None,
        effort=None,
        sandbox=None,
        timeout=None,
        dry_run=True,
        passthrough=(),
    )

    request = captured["request"]
    assert request.session.source_execution_cwd == "/tmp/resume-cwd"
    assert request.session.source_claude_config_dir == "/tmp/resume-claude-config"


def test_run_primary_launch_fork_threads_source_claude_config_dir_into_session_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        primary_launch,
        "resolve_session_reference",
        lambda _project_root, _ref: _resolved_reference(
            source_execution_cwd="/tmp/fork-cwd",
            source_claude_config_dir="/tmp/fork-claude-config",
        ),
    )

    captured: dict[str, object] = {}

    def _fake_launch_primary(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(
            exit_code=0,
            command=("claude", "--resume", "session-1"),
            continue_ref="session-2",
            warning=None,
        )

    monkeypatch.setattr(primary_launch, "launch_primary", _fake_launch_primary)

    primary_launch.run_primary_launch(
        project_root=tmp_path,
        continue_ref=None,
        fork_ref="session-1",
        model="",
        harness=None,
        agent=None,
        work="",
        yolo=False,
        approval=None,
        autocompact=None,
        effort=None,
        sandbox=None,
        timeout=None,
        dry_run=True,
        passthrough=(),
    )

    request = captured["request"]
    assert request.session.source_execution_cwd == "/tmp/fork-cwd"
    assert request.session.source_claude_config_dir == "/tmp/fork-claude-config"
