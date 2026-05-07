"""Regression tests for service-path startup classifications."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meridian.cli import chat_cmd
from meridian.cli.startup.catalog import COMMAND_CATALOG
from meridian.cli.startup.policy import StartupClass, StateRequirement, TelemetryMode


@pytest.mark.parametrize(
    "path",
    [
        ("chat", "ls"),
        ("chat", "show"),
        ("chat", "log"),
        ("chat", "close"),
    ],
)
def test_chat_management_commands_are_read_only_clients(path: tuple[str, ...]) -> None:
    descriptor = COMMAND_CATALOG.get(path)

    assert descriptor is not None
    assert descriptor.startup_class == StartupClass.CLIENT_READ
    assert descriptor.state_requirement == StateRequirement.RUNTIME_READ
    assert descriptor.telemetry_mode == TelemetryMode.NONE


@pytest.mark.parametrize("path", [("chat",), ("streaming", "serve")])
def test_interactive_services_are_runtime_writing(path: tuple[str, ...]) -> None:
    descriptor = COMMAND_CATALOG.get(path)

    assert descriptor is not None
    assert descriptor.startup_class == StartupClass.SERVICE_RUNTIME
    assert descriptor.state_requirement == StateRequirement.RUNTIME_WRITE
    assert descriptor.telemetry_mode == TelemetryMode.SEGMENT


def test_mcp_serve_is_rootless_stderr_telemetry() -> None:
    descriptor = COMMAND_CATALOG.get(("serve",))

    assert descriptor is not None
    assert descriptor.startup_class == StartupClass.SERVICE_ROOTLESS
    assert descriptor.state_requirement == StateRequirement.NONE
    assert descriptor.telemetry_mode == TelemetryMode.STDERR


@pytest.mark.parametrize(
    ("command", "args", "response"),
    [
        (chat_cmd._chat_ls, (), {"chats": []}),
        (chat_cmd._chat_show, ("c1",), {"chat_id": "c1", "state": "idle", "events": []}),
        (chat_cmd._chat_log, ("c1",), {"events": []}),
        (chat_cmd._chat_close, ("c1",), {"status": "accepted"}),
    ],
)
def test_chat_management_commands_use_runtime_read_preparation(
    monkeypatch: pytest.MonkeyPatch,
    command: Any,
    args: tuple[str, ...],
    response: dict[str, object],
) -> None:
    calls: list[str] = []

    def fake_runtime_read(project_root: Path) -> SimpleNamespace:
        calls.append(f"read:{project_root}")
        return SimpleNamespace(project_root=project_root, runtime_root=None, config=None)

    def fake_runtime_write(project_root: Path) -> None:
        calls.append(f"write:{project_root}")

    def fake_request_json(_method: str, path: str, *, url: str | None) -> dict[str, object]:
        if path.endswith("/state"):
            return {"chat_id": "c1", "state": "idle"}
        return response

    monkeypatch.setattr(chat_cmd, "resolve_project_root", lambda: Path("/repo"))
    monkeypatch.setattr(chat_cmd, "prepare_for_runtime_read", fake_runtime_read)
    monkeypatch.setattr(chat_cmd, "prepare_for_runtime_write", fake_runtime_write)
    monkeypatch.setattr(chat_cmd, "_request_json", fake_request_json)

    command(*args)

    assert calls == ["read:/repo"]
