# qa-validated: test-suite-redesign
"""Split-root connection regression tests for harness subprocess cwd handling."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import claude_ws, codex_ws
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.harness.connections.codex_ws import CodexConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 12345
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _build_spec() -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True)
    )


@pytest.mark.asyncio
async def test_claude_connection_launches_subprocess_from_control_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    control_root = tmp_path / "project"
    control_root.mkdir(parents=True)
    task_cwd = tmp_path / "task"
    task_cwd.mkdir(parents=True)

    captured: dict[str, object] = {}

    async def _fake_create_subprocess_exec(
        *command: str,
        cwd: str,
        env: Mapping[str, str],
        **_kwargs: object,
    ) -> _FakeProcess:
        captured["command"] = tuple(command)
        captured["cwd"] = cwd
        captured["env"] = dict(env)
        return _FakeProcess()

    monkeypatch.setattr(claude_ws, "inherit_child_env", lambda _base, overrides, blocked: overrides)
    monkeypatch.setattr(claude_ws.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    connection = ClaudeConnection()
    monkeypatch.setattr(connection, "_build_command", lambda _config, _spec: ["claude", "--test"])
    config = ConnectionConfig(
        spawn_id=SpawnId("p-claude-cwd"),
        harness_id=HarnessId.CLAUDE,
        prompt="hi",
        control_root=control_root,
        task_cwd=task_cwd,
        env_overrides={"MERIDIAN_TEST": "1"},
    )

    await connection._start_subprocess(config, _build_spec())
    await connection._cleanup_resources(terminate_process=False)

    assert captured["cwd"] == str(control_root)


@pytest.mark.asyncio
async def test_codex_connection_launches_subprocess_from_task_cwd_when_provided(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    control_root = tmp_path / "project"
    control_root.mkdir(parents=True)
    task_cwd = tmp_path / "task"
    task_cwd.mkdir(parents=True)

    captured: dict[str, object] = {}

    async def _fake_create_subprocess_exec(
        *command: str,
        cwd: str,
        env: Mapping[str, str],
        **_kwargs: object,
    ) -> _FakeProcess:
        captured["command"] = tuple(command)
        captured["cwd"] = cwd
        captured["env"] = dict(env)
        return _FakeProcess()

    async def _fake_connect_with_retry(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("stop-after-launch")

    async def _noop_cleanup(*, mark_stopped: bool) -> None:
        _ = mark_stopped

    monkeypatch.setattr(codex_ws, "inherit_child_env", lambda _base, overrides: overrides)
    monkeypatch.setattr(
        codex_ws,
        "project_managed_primary_backend_command",
        lambda _harness_id, _spec, host, port: ["codex", "app-server", host, str(port)],
    )
    monkeypatch.setattr(codex_ws.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    connection = CodexConnection()
    monkeypatch.setattr(connection, "_connect_with_retry", _fake_connect_with_retry)
    monkeypatch.setattr(connection, "_cleanup_resources", _noop_cleanup)
    config = ConnectionConfig(
        spawn_id=SpawnId("p-codex-cwd"),
        harness_id=HarnessId.CODEX,
        prompt="hi",
        control_root=control_root,
        task_cwd=task_cwd,
        env_overrides={"MERIDIAN_TEST": "1"},
        ws_port=19091,
    )

    with pytest.raises(RuntimeError, match="stop-after-launch"):
        await connection.start(config, _build_spec())

    if connection._stderr_handle is not None:
        connection._stderr_handle.close()
        connection._stderr_handle = None

    assert captured["cwd"] == str(task_cwd)


@pytest.mark.asyncio
async def test_codex_connection_launches_subprocess_from_control_root_without_task_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    control_root = tmp_path / "project"
    control_root.mkdir(parents=True)

    captured: dict[str, object] = {}

    async def _fake_create_subprocess_exec(
        *command: str,
        cwd: str,
        env: Mapping[str, str],
        **_kwargs: object,
    ) -> _FakeProcess:
        captured["command"] = tuple(command)
        captured["cwd"] = cwd
        captured["env"] = dict(env)
        return _FakeProcess()

    async def _fake_connect_with_retry(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("stop-after-launch")

    async def _noop_cleanup(*, mark_stopped: bool) -> None:
        _ = mark_stopped

    monkeypatch.setattr(codex_ws, "inherit_child_env", lambda _base, overrides: overrides)
    monkeypatch.setattr(
        codex_ws,
        "project_managed_primary_backend_command",
        lambda _harness_id, _spec, host, port: ["codex", "app-server", host, str(port)],
    )
    monkeypatch.setattr(codex_ws.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    connection = CodexConnection()
    monkeypatch.setattr(connection, "_connect_with_retry", _fake_connect_with_retry)
    monkeypatch.setattr(connection, "_cleanup_resources", _noop_cleanup)
    config = ConnectionConfig(
        spawn_id=SpawnId("p-codex-control-cwd"),
        harness_id=HarnessId.CODEX,
        prompt="hi",
        control_root=control_root,
        task_cwd=None,
        env_overrides={"MERIDIAN_TEST": "1"},
        ws_port=19092,
    )

    with pytest.raises(RuntimeError, match="stop-after-launch"):
        await connection.start(config, _build_spec())

    if connection._stderr_handle is not None:
        connection._stderr_handle.close()
        connection._stderr_handle = None

    assert captured["cwd"] == str(control_root)


def test_codex_managed_bootstrap_request_uses_task_cwd_when_provided(tmp_path: Path) -> None:
    control_root = tmp_path / "project"
    control_root.mkdir(parents=True)
    task_cwd = tmp_path / "task"
    task_cwd.mkdir(parents=True)

    connection = CodexConnection()
    connection._config = ConnectionConfig(
        spawn_id=SpawnId("p-codex-bootstrap-cwd"),
        harness_id=HarnessId.CODEX,
        prompt="hi",
        control_root=control_root,
        task_cwd=task_cwd,
        env_overrides={},
    )
    method, payload = connection._thread_bootstrap_request(_build_spec())

    assert method == "thread/start"
    assert payload["cwd"] == str(task_cwd)


def test_codex_managed_bootstrap_request_uses_control_root_without_task_cwd(tmp_path: Path) -> None:
    control_root = tmp_path / "project"
    control_root.mkdir(parents=True)

    connection = CodexConnection()
    connection._config = ConnectionConfig(
        spawn_id=SpawnId("p-codex-bootstrap-control-cwd"),
        harness_id=HarnessId.CODEX,
        prompt="hi",
        control_root=control_root,
        task_cwd=None,
        env_overrides={},
    )
    method, payload = connection._thread_bootstrap_request(_build_spec())

    assert method == "thread/start"
    assert payload["cwd"] == str(control_root)
