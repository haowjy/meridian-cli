from __future__ import annotations

import json
import subprocess
from pathlib import Path, PureWindowsPath
from typing import cast

import pytest

from meridian.lib.core.types import HarnessId
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


def _valid_bundle_payload() -> dict[str, object]:
    return {
        "version": 2,
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
    }


def _resolve_with_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object] | None = None,
    *,
    agent: str | None = None,
    project_root: str | Path | None = None,
):
    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: "mars")
    resolved_payload = payload if payload is not None else _valid_bundle_payload()
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: _completed(stdout=json.dumps(resolved_payload)),
    )
    return request_and_resolve(
        BundleRequest(
            agent=agent,
            project_root=Path(project_root) if project_root is not None else Path("/tmp/project"),
        ),
        harness_registry=get_default_harness_registry(),
    )


def test_request_and_resolve_builds_expected_mars_command(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        payload = _valid_bundle_payload()
        return _completed(stdout=json.dumps(payload))

    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr("subprocess.run", fake_run)

    project_root = Path("project").resolve()
    request = BundleRequest(
        agent="coder",
        project_root=project_root,
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
    assert bundle.model == "gpt-5.5"
    assert bundle.model_token == "gpt55"
    assert bundle.harness is HarnessId.OPENCODE
    assert bundle.harness_model == "openai/gpt-5.5"


def test_request_and_resolve_accepts_project_default_bundle_without_local_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        payload = _valid_bundle_payload()
        payload["routing"] = {
            "model": "gpt-5.3-codex",
            "model_token": "gpt-5.3-codex",
            "harness": "codex",
            "harness_model": "gpt-5.3-codex",
        }
        payload["provenance"] = {
            "model_source": "project",
            "harness_source": "project",
        }
        return _completed(stdout=json.dumps(payload))

    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr("subprocess.run", fake_run)

    project_root = Path("project").resolve()
    bundle = request_and_resolve(
        BundleRequest(agent=None, project_root=project_root),
        harness_registry=get_default_harness_registry(),
    )

    command = recorded["command"]
    assert command == [
        "mars",
        "build",
        "launch-bundle",
        "--json",
        "--root",
        str(project_root),
    ]
    assert bundle.model == "gpt-5.3-codex"
    assert bundle.model_token == "gpt-5.3-codex"
    assert bundle.harness is HarnessId.CODEX
    assert bundle.provenance["model_source"] == "project"
    assert bundle.provenance["harness_source"] == "project"


def test_request_and_resolve_uses_native_windows_root_path(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        return _completed(stdout=json.dumps(_valid_bundle_payload()))

    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr("subprocess.run", fake_run)

    windows_root = cast("Path", PureWindowsPath(r"C:\Users\Jane\repo"))
    request_and_resolve(
        BundleRequest(agent="coder", project_root=windows_root),
        harness_registry=get_default_harness_registry(),
    )

    command = recorded["command"]
    assert isinstance(command, list)
    assert "--root" in command
    root_index = command.index("--root")
    assert command[root_index + 1] == r"C:\Users\Jane\repo"


def test_request_and_resolve_reports_missing_mars_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: None)

    with pytest.raises(RuntimeError, match=r"mars >= 0\.6\.1"):
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

    with pytest.raises(RuntimeError, match=r"mars >= 0\.6\.1"):
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


def test_request_and_resolve_reports_non_object_json_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: "mars")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _completed(stdout="[]"))

    with pytest.raises(RuntimeError, match="non-object JSON payload"):
        request_and_resolve(
            BundleRequest(agent=None, project_root=Path("/tmp/project")),
            harness_registry=get_default_harness_registry(),
        )


def test_request_and_resolve_reports_unsupported_schema_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _valid_bundle_payload()
    payload["version"] = 99

    with pytest.raises(RuntimeError, match=r"schema version 99 is unsupported.*mars >= 0\.6\.1"):
        _resolve_with_payload(monkeypatch, payload)


def test_request_and_resolve_accepts_empty_model_for_harness_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _valid_bundle_payload()
    payload["routing"] = {"model": "", "harness": "opencode"}

    bundle = _resolve_with_payload(monkeypatch, payload)
    assert bundle.model == ""
    assert bundle.model_token == ""
    assert bundle.harness is HarnessId.OPENCODE


def test_request_and_resolve_reports_missing_routing_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _valid_bundle_payload()
    payload["routing"] = {"model": "gpt-5.5"}

    with pytest.raises(RuntimeError, match=r"routing\.harness is empty"):
        _resolve_with_payload(monkeypatch, payload)


@pytest.mark.parametrize(
    ("field_name", "field_value", "message"),
    [
        ("routing", [], r"'routing' must be an object"),
        ("tools", False, r"'tools' must be an object when present"),
    ],
)
def test_request_and_resolve_reports_invalid_nested_bundle_sections(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    field_value: object,
    message: str,
) -> None:
    payload = _valid_bundle_payload()
    payload[field_name] = field_value

    with pytest.raises(RuntimeError, match=message):
        _resolve_with_payload(monkeypatch, payload)


def test_request_and_resolve_reports_unknown_routing_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _valid_bundle_payload()
    payload["routing"] = {
        "model": "gpt-5.5",
        "model_token": "gpt55",
        "harness": "definitely-not-a-harness",
    }

    with pytest.raises(ValueError, match="unsupported routing\\.harness"):
        _resolve_with_payload(monkeypatch, payload)


def test_request_and_resolve_reports_subprocess_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: "mars")

    def raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError("mars")

    monkeypatch.setattr("subprocess.run", raise_file_not_found)

    with pytest.raises(RuntimeError, match="mars binary was not found"):
        request_and_resolve(
            BundleRequest(agent=None, project_root=Path("/tmp/project")),
            harness_registry=get_default_harness_registry(),
        )


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (subprocess.TimeoutExpired(cmd="mars", timeout=60), "failed to execute"),
    ],
)
def test_request_and_resolve_reports_subprocess_execution_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    message: str,
) -> None:
    monkeypatch.setattr("meridian.lib.launch.bundle_adapter._resolve_mars_binary", lambda: "mars")

    def raise_error(*args, **kwargs):
        raise error

    monkeypatch.setattr("subprocess.run", raise_error)

    with pytest.raises(RuntimeError, match=message):
        request_and_resolve(
            BundleRequest(agent=None, project_root=Path("/tmp/project")),
            harness_registry=get_default_harness_registry(),
        )
