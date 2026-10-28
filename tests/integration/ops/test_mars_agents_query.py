"""Integration coverage for mars agent/subagent query helpers."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from meridian.lib.catalog.mars_roots import MarsRoots
from meridian.lib.ops import mars as mars_module
from meridian.lib.ops.mars import MarsAgentEntry, mars_agent_subagents, mars_list_subagents

pytestmark = pytest.mark.slow

_FAKE_MARS = "/fake/mars"
_PROJECT_ROOT = Path("/fake/project")
_USER_ROOT = Path("/fake/user-mars")


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
    monkeypatch.setattr(
        mars_module,
        "_prepare_mars_roots",
        lambda _project_root, *, executable: MarsRoots(
            primary=_PROJECT_ROOT,
            scope="project",
            user=_USER_ROOT,
        ),
    )
    monkeypatch.setattr(
        mars_module,
        "mars_fallback_args",
        lambda _roots, *, executable: ("--fallback-root", _USER_ROOT.as_posix()),
    )


@pytest.mark.parametrize(
    ("envelope", "expected"),
    [
        pytest.param(
            {"name": "tech-lead", "subagents": ["reviewer", "coder", "reviewer"]},
            ("coder", "reviewer"),
            id="sorted-deduped",
        ),
        pytest.param({"name": "leaf", "subagents": []}, (), id="empty-list"),
        pytest.param({"name": "no-list"}, (), id="absent-field"),
    ],
)
def test_mars_agent_subagents_parses_show_envelope(
    monkeypatch: pytest.MonkeyPatch,
    envelope: dict[str, object],
    expected: tuple[str, ...],
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return _completed(stdout=json.dumps(envelope))

    _patch_mars_binary(monkeypatch)
    monkeypatch.setattr(mars_module.subprocess, "run", fake_run)

    result = mars_agent_subagents(_PROJECT_ROOT, str(envelope["name"]))

    assert result == expected
    assert recorded["command"] == [
        _FAKE_MARS,
        "agents",
        "show",
        envelope["name"],
        "--json",
        "--root",
        _PROJECT_ROOT.as_posix(),
        "--fallback-root",
        _USER_ROOT.as_posix(),
    ]


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
                        {
                            "name": "reviewer",
                            "mode": "subagent",
                            "scope": "fallback",
                            "source_path": "/fake/user-mars/.mars/agents/reviewer.md",
                        },
                        {"name": "coder", "mode": "subagent", "scope": "root"},
                        {"name": "coder", "mode": "subagent"},
                        {"name": "tech-lead", "mode": "primary"},
                        {"name": "no-mode"},
                    ]
                }
            )
        )

    _patch_mars_binary(monkeypatch)
    monkeypatch.setattr(mars_module.subprocess, "run", fake_run)

    result = mars_list_subagents(_PROJECT_ROOT)

    # Only subagent-mode entries survive the client-side filter; primary and
    # mode-less entries are dropped.
    assert result == (
        MarsAgentEntry(name="coder", scope="root"),
        MarsAgentEntry(
            name="reviewer",
            scope="fallback",
            source_path="/fake/user-mars/.mars/agents/reviewer.md",
        ),
    )
    # The ``--mode`` filter is NOT passed to ``agents list`` (it belongs to the
    # parent ``mars agents`` command); filtering happens client-side.
    assert recorded["command"] == [
        _FAKE_MARS,
        "agents",
        "list",
        "--json",
        "--root",
        _PROJECT_ROOT.as_posix(),
        "--fallback-root",
        _USER_ROOT.as_posix(),
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
