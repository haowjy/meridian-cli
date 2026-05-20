from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from meridian.lib.harness.registry import get_default_harness_registry
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


def test_request_and_resolve_builds_expected_mars_command(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        payload = {
            "version": 1,
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
            "prompt_surface": {
                "system_instruction": "# Report",
                "supplemental_documents": [
                    {
                        "kind": "skill",
                        "name": "shared-workspace",
                        "content": "# Skill: shared-workspace\\n\\nBody",
                        "skill_type": "guardrail",
                    }
                ],
                "inventory_prompt": "# Meridian Agents",
            },
            "tools": {
                "allowed": ["read"],
                "disallowed": ["rm"],
                "mcp": ["github"],
            },
            "skills_metadata": {
                "loaded": ["shared-workspace"],
                "missing": ["missing-skill"],
            },
            "provenance": {
                "model_source": "cli",
                "harness_source": "cli",
                "effort_source": "cli",
            },
            "warnings": ["bundle warning"],
        }
        return _completed(stdout=json.dumps(payload))

    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr("subprocess.run", fake_run)

    request = BundleRequest(
        agent="coder",
        project_root=Path("/tmp/project"),
        model_override="gpt55",
        harness_override="opencode",
        effort_override="high",
        approval_override="auto",
        sandbox_override="workspace-write",
        extra_skills=("shared-workspace", "review"),
    )
    bundle = request_and_resolve(request, harness_registry=get_default_harness_registry())

    assert recorded["command"] == [
        "mars",
        "build",
        "launch-bundle",
        "--json",
        "--root",
        "/tmp/project",
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
    assert bundle.model == "gpt-5.5"
    assert bundle.model_token == "gpt55"
    assert bundle.harness_model == "openai/gpt-5.5"
    assert bundle.prompt_surface_inventory_prompt == "# Meridian Agents"
    assert bundle.tools_allowed == ("read",)
    assert bundle.skills_loaded == ("shared-workspace",)
    assert bundle.skills_missing == ("missing-skill",)


def test_request_and_resolve_reports_missing_mars_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: None)

    with pytest.raises(RuntimeError, match=r"mars >= 0\.4\.8rc3"):
        request_and_resolve(
            BundleRequest(agent=None, project_root=Path("/tmp/project")),
            harness_registry=get_default_harness_registry(),
        )


def test_request_and_resolve_reports_unsupported_launch_bundle_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: _completed(
            returncode=2,
            stderr="error: unrecognized subcommand 'launch-bundle'",
        ),
    )

    with pytest.raises(RuntimeError, match=r"mars >= 0\.4\.8rc3"):
        request_and_resolve(
            BundleRequest(agent=None, project_root=Path("/tmp/project")),
            harness_registry=get_default_harness_registry(),
        )


def test_request_and_resolve_reports_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _completed(stdout="not-json"))

    with pytest.raises(RuntimeError, match="invalid JSON"):
        request_and_resolve(
            BundleRequest(agent=None, project_root=Path("/tmp/project")),
            harness_registry=get_default_harness_registry(),
        )
