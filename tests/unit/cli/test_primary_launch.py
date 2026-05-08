from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
        "recovery": None,
    }
    payload.update(overrides)
    obj = type("Resolved", (), payload)()
    recorded = getattr(obj, "harness_session_id", None)
    obj.effective_harness_session_id = recorded
    obj.authoritative_harness_session_id = recorded
    return obj


def test_run_primary_launch_rejects_cross_harness_continue_and_fork(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        primary_launch,
        "resolve_session_reference",
        lambda _project_root, _ref: _resolved_reference(harness="claude"),
    )

    for continue_ref, fork_ref, expected in (
        ("session-1", None, "Cannot continue across harnesses"),
        (None, "session-1", "Cannot fork across harnesses"),
    ):
        with pytest.raises(ValueError, match=expected):
            primary_launch.run_primary_launch(
                project_root=tmp_path,
                continue_ref=continue_ref,
                fork_ref=fork_ref,
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
                dry_run=False,
                passthrough=(),
            )


def test_run_primary_launch_resume_shapes_session_request_from_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        primary_launch,
        "resolve_session_reference",
        lambda _project_root, _ref: _resolved_reference(
            source_chat_id="c-resume",
            source_execution_cwd="/tmp/resume-cwd",
            source_claude_config_dir="/tmp/resume-claude-config",
            tracked=True,
        ),
    )
    captured: dict[str, object] = {}

    def _fake_launch_primary(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(exit_code=0, command=(), continue_ref="session-1", warning=None)

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
        dry_run=False,
        passthrough=(),
    )

    session = cast("Any", captured["request"]).session
    assert session.requested_harness_session_id == "session-1"
    assert session.continue_harness == "claude"
    assert session.continue_chat_id == "c-resume"
    assert session.source_execution_cwd == "/tmp/resume-cwd"
    assert session.source_claude_config_dir == "/tmp/resume-claude-config"
    assert session.continue_source_tracked is True
    assert session.continue_source_ref == "session-1"


def test_run_primary_launch_fork_shapes_session_request_from_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        primary_launch,
        "resolve_session_reference",
        lambda _project_root, _ref: _resolved_reference(
            source_chat_id="c-fork",
            source_execution_cwd="/tmp/fork-cwd",
            source_claude_config_dir="/tmp/fork-claude-config",
            tracked=True,
        ),
    )
    captured: dict[str, object] = {}

    def _fake_launch_primary(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(exit_code=0, command=(), continue_ref="session-2", warning=None)

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
        dry_run=False,
        passthrough=(),
    )

    session = cast("Any", captured["request"]).session
    assert session.requested_harness_session_id == "session-1"
    assert session.continue_harness == "claude"
    assert session.continue_fork is True
    assert session.forked_from_chat_id == "c-fork"
    assert session.source_execution_cwd == "/tmp/fork-cwd"
    assert session.source_claude_config_dir == "/tmp/fork-claude-config"
    assert session.continue_source_tracked is True
    assert session.continue_source_ref == "session-1"


def test_run_primary_launch_resume_failure_uses_failure_wording(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        primary_launch,
        "resolve_session_reference",
        lambda _project_root, _ref: _resolved_reference(),
    )
    monkeypatch.setattr(
        primary_launch,
        "launch_primary",
        lambda **_kwargs: SimpleNamespace(
            exit_code=2,
            command=(),
            continue_ref=None,
            warning="No conversation found with session ID: session-1",
        ),
    )

    result = primary_launch.run_primary_launch(
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
        dry_run=False,
        passthrough=(),
    )

    assert result.exit_code == 2
    assert result.message == "Session resume failed."


def test_run_primary_launch_fork_failure_uses_failure_wording(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        primary_launch,
        "resolve_session_reference",
        lambda _project_root, _ref: _resolved_reference(),
    )
    monkeypatch.setattr(
        primary_launch,
        "launch_primary",
        lambda **_kwargs: SimpleNamespace(
            exit_code=2,
            command=(),
            continue_ref=None,
            warning="Fork failed",
        ),
    )

    result = primary_launch.run_primary_launch(
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
        dry_run=False,
        passthrough=(),
    )

    assert result.exit_code == 2
    assert result.message == "Session fork failed."
