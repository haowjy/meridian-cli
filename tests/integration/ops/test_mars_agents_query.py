"""Integration coverage for mars agent/subagent query helpers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from meridian.lib.catalog.mars_roots import MarsRoots
from meridian.lib.ops import mars as mars_module
from meridian.lib.ops.mars import MarsAgentEntry, mars_list_agents

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


def test_mars_list_agents_parses_list_envelope(
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
                            "description": "  Reviews the change.  ",
                            "model-invocable": False,
                            "native-harnesses": ["claude"],
                            "fanout": True,
                        },
                        {"name": "coder", "mode": "subagent", "scope": "root"},
                        {"name": "coder", "mode": "subagent"},
                        {"name": "tech-lead", "mode": "primary", "subagents": ["coder"]},
                    ]
                }
            )
        )

    _patch_mars_binary(monkeypatch)
    monkeypatch.setattr(mars_module.subprocess, "run", fake_run)

    result = mars_list_agents(_PROJECT_ROOT)

    # Every mode is listed; a row without routing fields reads as model-invocable,
    # not native, not fanout.
    assert result == (
        MarsAgentEntry(name="coder", scope="root", mode="subagent"),
        MarsAgentEntry(
            name="reviewer",
            scope="fallback",
            source_path="/fake/user-mars/.mars/agents/reviewer.md",
            description="Reviews the change.",
            mode="subagent",
            model_invocable=False,
            native_harnesses=("claude",),
            fanout=True,
        ),
        MarsAgentEntry(name="tech-lead", scope="root", mode="primary", subagents=("coder",)),
    )
    # No ``--mode`` filter: callers need every mode, so the full list is read.
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
def test_mars_list_agents_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    returncode: int,
) -> None:
    def fake_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(returncode=returncode, stdout=stdout)

    _patch_mars_binary(monkeypatch)
    monkeypatch.setattr(mars_module.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="mars agents list"):
        mars_list_agents(_PROJECT_ROOT)
