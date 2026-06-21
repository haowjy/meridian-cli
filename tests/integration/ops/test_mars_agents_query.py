"""Integration coverage for mars agent/subagent query helpers."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from meridian.lib.ops import mars as mars_module
from meridian.lib.ops.mars import mars_agent_subagents, mars_list_subagents

pytestmark = pytest.mark.slow

_FAKE_MARS = "/fake/mars"
_PROJECT_ROOT = Path("/fake/project")


def _completed(
    *,
    returncode: int = 0,
    stdout: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def _patch_mars_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mars_module, "resolve_mars_executable", lambda: _FAKE_MARS)


def test_mars_agent_subagents_parses_show_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return _completed(
            stdout=json.dumps(
                {
                    "name": "tech-lead",
                    "subagents": ["reviewer", "coder", "reviewer"],
                }
            )
        )

    _patch_mars_binary(monkeypatch)
    monkeypatch.setattr(mars_module.subprocess, "run", fake_run)

    result = mars_agent_subagents(_PROJECT_ROOT, "tech-lead")

    assert result == ("coder", "reviewer")
    assert recorded["command"] == [
        _FAKE_MARS,
        "agents",
        "show",
        "tech-lead",
        "--json",
        "--root",
        _PROJECT_ROOT.as_posix(),
    ]
    assert recorded["kwargs"] == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 60,
    }


def test_mars_agent_subagents_empty_or_absent_subagents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "leaf" in command:
            return _completed(stdout=json.dumps({"name": "leaf", "subagents": []}))
        return _completed(stdout=json.dumps({"name": "no-list"}))

    _patch_mars_binary(monkeypatch)
    monkeypatch.setattr(mars_module.subprocess, "run", fake_run)

    assert mars_agent_subagents(_PROJECT_ROOT, "leaf") == ()
    assert mars_agent_subagents(_PROJECT_ROOT, "no-list") == ()


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        ("", 1),
        ("not-json", 0),
        (json.dumps([]), 0),
    ],
)
def test_mars_agent_subagents_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stdout: str,
    returncode: int,
) -> None:
    def fake_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(returncode=returncode, stdout=stdout)

    _patch_mars_binary(monkeypatch)
    monkeypatch.setattr(mars_module.subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING, logger="meridian.lib.ops.mars"):
        result = mars_agent_subagents(_PROJECT_ROOT, "tech-lead")

    assert result is None
    assert caplog.records


def test_mars_list_subagents_parses_list_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return _completed(
            stdout=json.dumps(
                {
                    "agents": [
                        {"name": "reviewer"},
                        {"name": "coder"},
                        {"name": "coder"},
                    ]
                }
            )
        )

    _patch_mars_binary(monkeypatch)
    monkeypatch.setattr(mars_module.subprocess, "run", fake_run)

    result = mars_list_subagents(_PROJECT_ROOT)

    assert result == ("coder", "reviewer")
    assert recorded["command"] == [
        _FAKE_MARS,
        "agents",
        "list",
        "--mode",
        "subagent",
        "--json",
        "--root",
        _PROJECT_ROOT.as_posix(),
    ]
    assert recorded["kwargs"] == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 60,
    }


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        ("", 1),
        ("not-json", 0),
        (json.dumps([]), 0),
    ],
)
def test_mars_list_subagents_failure_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stdout: str,
    returncode: int,
) -> None:
    def fake_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(returncode=returncode, stdout=stdout)

    _patch_mars_binary(monkeypatch)
    monkeypatch.setattr(mars_module.subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING, logger="meridian.lib.ops.mars"):
        result = mars_list_subagents(_PROJECT_ROOT)

    assert result == ()
    assert caplog.records


def test_mars_helpers_no_warning_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(mars_module, "resolve_mars_executable", lambda: None)

    with caplog.at_level(logging.WARNING, logger="meridian.lib.ops.mars"):
        assert mars_agent_subagents(_PROJECT_ROOT, "tech-lead") is None
        assert mars_list_subagents(_PROJECT_ROOT) == ()

    assert not caplog.records
