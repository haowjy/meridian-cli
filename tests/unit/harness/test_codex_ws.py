from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from meridian.lib.harness.connections import codex_ws
from meridian.lib.harness.connections.base import AutoAcceptHandler, PrimaryRuntimeRequestPolicy
from meridian.lib.harness.launch_spec import CodexLaunchSpec
from meridian.lib.harness.projections.project_codex_common import (
    HarnessCapabilityMismatch,
    map_codex_approval_policy,
)
from meridian.lib.harness.projections.project_codex_streaming import (
    project_codex_spec_to_appserver_command,
    project_codex_spec_to_thread_request,
)
from meridian.lib.safety.permissions import (
    PermissionConfig,
    TieredPermissionResolver,
    UnsafeNoOpPermissionResolver,
)


def _values_for_setting(command: list[str], key: str) -> list[str]:
    values: list[str] = []
    for index, token in enumerate(command):
        if token != "-c":
            continue
        if index + 1 >= len(command):
            continue
        setting = command[index + 1]
        prefix = f"{key}="
        if setting.startswith(prefix):
            values.append(setting[len(prefix) :])
    return values


def test_codex_ws_primary_runtime_request_policy_none_keeps_auto_accept() -> None:
    connection = codex_ws.CodexConnection()

    connection.configure_primary_runtime_requests(policy=PrimaryRuntimeRequestPolicy.NONE)

    assert isinstance(connection._request_handler, AutoAcceptHandler)


def test_codex_streaming_projection_builds_appserver_command_and_logs_ignored_report_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = CodexLaunchSpec(
        permission_resolver=TieredPermissionResolver(
            config=PermissionConfig(sandbox="read-only", approval="auto")
        ),
        report_output_path="report.md",
        extra_args=("--invalid-flag",),
        projected_roots=(Path("/tmp/root-a"), Path("/tmp/root b")),
    )

    with caplog.at_level(
        logging.DEBUG, logger="meridian.lib.harness.projections.project_codex_streaming"
    ):
        command = project_codex_spec_to_appserver_command(
            spec,
            host="127.0.0.1",
            port=7777,
        )

    assert command[:4] == ["codex", "app-server", "--listen", "ws://127.0.0.1:7777"]
    assert _values_for_setting(command, "sandbox_mode") == ['"read-only"']
    assert _values_for_setting(command, "approval_policy") == ['"on-request"']
    assert _values_for_setting(command, "sandbox_workspace_write.writable_roots") == [
        '["/tmp/root-a", "/tmp/root b"]'
    ]
    assert command[-1:] == ["--invalid-flag"]
    assert (
        "Codex streaming ignores report_output_path; reports extracted from artifacts"
        in caplog.text
    )


@pytest.mark.parametrize(
    ("spec", "cwd", "expected_method", "expected_payload"),
    [
        (
            CodexLaunchSpec(
                prompt="hello",
                model="gpt-5.3-codex",
                effort="high",
                permission_resolver=TieredPermissionResolver(
                    config=PermissionConfig(sandbox="read-only", approval="auto")
                ),
            ),
            "/tmp/start",
            "thread/start",
            {
                "cwd": "/tmp/start",
                "model": "gpt-5.3-codex",
                "config": {"model_reasoning_effort": "high"},
                "approvalPolicy": "on-request",
                "sandbox": "read-only",
            },
        ),
        (
            CodexLaunchSpec(
                prompt="hello",
                model="gpt-5.3-codex",
                continue_session_id="thread-123",
                permission_resolver=TieredPermissionResolver(
                    config=PermissionConfig(approval="confirm")
                ),
            ),
            "/tmp/resume",
            "thread/resume",
            {
                "cwd": "/tmp/resume",
                "model": "gpt-5.3-codex",
                "approvalPolicy": "untrusted",
                "threadId": "thread-123",
            },
        ),
        (
            CodexLaunchSpec(
                prompt="hello",
                model="gpt-5.3-codex",
                continue_session_id="thread-123",
                continue_fork=True,
                permission_resolver=TieredPermissionResolver(
                    config=PermissionConfig(sandbox="workspace-write", approval="default")
                ),
            ),
            "/tmp/fork",
            "thread/fork",
            {
                "cwd": "/tmp/fork",
                "model": "gpt-5.3-codex",
                "threadId": "thread-123",
                "sandbox": "workspace-write",
                "ephemeral": False,
            },
        ),
        (
            CodexLaunchSpec(
                prompt="hello",
                model="gpt-5.3-codex",
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            ),
            "/tmp/default",
            "thread/start",
            {
                "cwd": "/tmp/default",
                "model": "gpt-5.3-codex",
            },
        ),
    ],
    ids=["start-with-policy", "resume", "fork", "start-default"],
)
def test_codex_ws_thread_request_projection(
    spec: CodexLaunchSpec,
    cwd: str,
    expected_method: str,
    expected_payload: dict[str, object],
) -> None:
    method, payload = project_codex_spec_to_thread_request(spec, cwd=cwd)

    assert method == expected_method
    assert payload == expected_payload


def test_codex_permission_mapping_fails_closed_on_unsupported_mode() -> None:
    with pytest.raises(HarnessCapabilityMismatch, match="approval mode 'unsupported'"):
        map_codex_approval_policy("unsupported")


@pytest.mark.asyncio
async def test_codex_cleanup_resources_uses_scope_handle_for_live_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = codex_ws.CodexConnection()
    terminate_calls: list[tuple[float, str]] = []

    class _FakeScopeHandle:
        async def terminate(self, *, grace_seconds: float, reason: str) -> None:
            terminate_calls.append((grace_seconds, reason))

    class _FakeProcess:
        returncode = None

        def terminate(self) -> None:
            raise AssertionError("scope-handle cleanup should replace direct terminate()")

        def kill(self) -> None:
            raise AssertionError("scope-handle cleanup should replace direct kill()")

        async def wait(self) -> None:
            return None

    async def _close_ws() -> None:
        return None

    async def _clear_stale_hitl_requests(*, reason: str) -> None:
        _ = reason

    monkeypatch.setattr(connection, "_close_ws", _close_ws)
    monkeypatch.setattr(connection, "_clear_stale_hitl_requests", _clear_stale_hitl_requests)
    monkeypatch.setattr(connection, "_close_log_handles", lambda: None)

    connection._reader_task = asyncio.create_task(asyncio.sleep(0))
    connection._scope_handle = _FakeScopeHandle()
    connection._process = _FakeProcess()

    await connection._cleanup_resources(mark_stopped=False)

    assert terminate_calls == [
        (codex_ws._STOP_WAIT_TIMEOUT_SECONDS, "stop_called"),
    ]
    assert connection._scope_handle is None
    assert connection._process is None
