from __future__ import annotations

import json
import subprocess
from pathlib import Path, PureWindowsPath
from typing import cast

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.bundle_adapter import BundleRequest, request_and_resolve


def _completed(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["mars"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _valid_bundle_payload() -> dict[str, object]:
    return {
        "version": 3,
        "routing": {
            "model": "gpt-5.5",
            "model_token": "gpt55",
            "harness": "opencode",
            "harness_model": "openai/gpt-5.5",
        },
        "execution_policy": {
            "effort": "high",
            "approval": "auto",
            "sandbox": "workspace-write",
        },
        "provenance": {
            "model_source": "cli",
            "harness_source": "cli",
            "effort_source": "cli",
        },
        "warnings": ["bundle warning"],
        "tools": {"allowed": ["webfetch"], "disallowed": ["shell"], "mcp": ["github"]},
        "skills": {
            "loaded": [{"name": "testing", "skill_type": "reference", "body": "body"}],
            "available": [{"name": "review", "skill_type": "workflow"}],
            "missing": ["absent"],
        },
    }


def test_request_and_resolve_builds_command_and_parses_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return _completed(stdout=json.dumps(_valid_bundle_payload()))

    monkeypatch.setattr(bundle_adapter, "_resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr(bundle_adapter.subprocess, "run", fake_run)

    project_root = Path("project").resolve()
    bundle = request_and_resolve(
        BundleRequest(
            agent="coder",
            project_root=project_root,
            model_override="gpt55",
            harness_override="opencode",
            effort_override="high",
            approval_override="auto",
            sandbox_override="workspace-write",
            extra_skills=("shared-workspace", "review"),
        ),
        harness_registry=get_default_harness_registry(),
    )

    assert recorded["command"] == [
        "mars",
        "build",
        "launch-bundle",
        "--json",
        "--root",
        str(project_root),
        "--agent",
        "coder",
        "--model",
        "gpt55",
        "--harness",
        "opencode",
        "--effort",
        "high",
        "--approval",
        "auto",
        "--sandbox",
        "workspace-write",
        "--skill",
        "shared-workspace",
        "--skill",
        "review",
    ]
    assert recorded["kwargs"] == {
        "capture_output": True,
        "text": True,
        "timeout": 60,
        "encoding": "utf-8",
        "errors": "replace",
    }
    assert bundle.model == "gpt-5.5"
    assert bundle.model_token == "gpt55"
    assert bundle.harness is HarnessId.OPENCODE
    assert bundle.harness_model == "openai/gpt-5.5"
    assert bundle.execution_policy.effort == "high"
    assert bundle.tools_allowed == ("webfetch",)
    assert bundle.tools_disallowed == ("shell",)
    assert bundle.tools_mcp == ("github",)
    assert bundle.skills_loaded[0].name == "testing"
    assert bundle.skills_available[0].name == "review"
    assert bundle.skills_missing == ("absent",)


def test_request_and_resolve_uses_native_windows_root_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        return _completed(stdout=json.dumps(_valid_bundle_payload()))

    monkeypatch.setattr(bundle_adapter, "_resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr(bundle_adapter.subprocess, "run", fake_run)

    request_and_resolve(
        BundleRequest(
            agent="coder",
            project_root=cast("Path", PureWindowsPath(r"C:\Users\Jane\repo")),
        ),
        harness_registry=get_default_harness_registry(),
    )

    command = cast("list[str]", recorded["command"])
    assert command[command.index("--root") + 1] == r"C:\Users\Jane\repo"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"version": 99}, "schema version 99 is unsupported"),
        ({"version": 3, "routing": [], "execution_policy": {}}, "'routing' must be an object"),
        (
            {"version": 3, "routing": {"harness": "bogus"}, "execution_policy": {}},
            "unsupported routing.harness 'bogus'",
        ),
    ],
)
def test_request_and_resolve_rejects_invalid_bundle_schema(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    expected: str,
) -> None:
    monkeypatch.setattr(bundle_adapter, "_resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr(
        bundle_adapter.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(stdout=json.dumps(payload)),
    )

    with pytest.raises((RuntimeError, ValueError), match=expected):
        request_and_resolve(
            BundleRequest(agent=None, project_root=Path("/tmp/project")),
            harness_registry=get_default_harness_registry(),
        )


def test_request_and_resolve_reports_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bundle_adapter, "_resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr(
        bundle_adapter.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(stdout="not-json"),
    )

    with pytest.raises(RuntimeError, match="invalid JSON output"):
        request_and_resolve(
            BundleRequest(agent=None, project_root=Path("/tmp/project")),
            harness_registry=get_default_harness_registry(),
        )


def test_request_and_resolve_reports_unavailable_launch_bundle_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bundle_adapter, "_resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr(
        bundle_adapter.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(
            stderr='{"error":"unknown command launch-bundle"}',
            returncode=2,
        ),
    )

    with pytest.raises(RuntimeError, match="launch-bundle command is unavailable"):
        request_and_resolve(
            BundleRequest(agent=None, project_root=Path("/tmp/project")),
            harness_registry=get_default_harness_registry(),
        )
