# qa-validated: test-suite-redesign
"""Unit tests for harness subprocess cwd handling (control_root vs task_cwd)."""

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
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn_store import start_spawn


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

    monkeypatch.setattr(claude_ws.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    connection = ClaudeConnection()
    monkeypatch.setattr(connection, "_build_command", lambda _config, _spec: ["claude", "--test"])
    spawn_id = SpawnId("p-claude-cwd")
    runtime_root = resolve_project_runtime_root_for_write(control_root)
    start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="chat-1",
        model="gpt-5.4",
        agent="coder",
        harness="claude",
        prompt="hi",
    )
    config = ConnectionConfig(
        spawn_id=spawn_id,
        harness_id=HarnessId.CLAUDE,
        prompt="hi",
        control_root=control_root,
        runtime_root=runtime_root,
        task_cwd=task_cwd,
        child_env={"MERIDIAN_TEST": "1"},
    )

    await connection._start_subprocess(config, _build_spec())
    await connection._cleanup_resources(terminate_process=False)

    assert captured["cwd"] == str(control_root)


async def _capture_codex_launch_cwd(
    monkeypatch: pytest.MonkeyPatch,
    *,
    control_root: Path,
    task_cwd: Path | None,
    ws_port: int,
) -> str:
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

    monkeypatch.setattr(
        codex_ws,
        "project_managed_primary_backend_command",
        lambda _harness_id, _spec, host, port: ["codex", "app-server", host, str(port)],
    )
    monkeypatch.setattr(codex_ws.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    connection = CodexConnection()
    monkeypatch.setattr(connection, "_connect_with_retry", _fake_connect_with_retry)
    monkeypatch.setattr(connection, "_cleanup_resources", _noop_cleanup)
    spawn_id = SpawnId(f"p-codex-{ws_port}")
    start_spawn(
        resolve_project_runtime_root_for_write(control_root),
        spawn_id=spawn_id,
        chat_id="chat-1",
        model="gpt-5.4",
        agent="tester",
        harness="codex",
        prompt="test",
        status="running",
    )
    config = ConnectionConfig(
        spawn_id=spawn_id,
        harness_id=HarnessId.CODEX,
        prompt="hi",
        control_root=control_root,
        task_cwd=task_cwd,
        child_env={
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "MERIDIAN_SPAWN_ID": str(spawn_id),
        },
        ws_port=ws_port,
    )

    with pytest.raises(RuntimeError, match="stop-after-launch"):
        await connection.start(config, _build_spec())

    if connection._stderr_handle is not None:
        connection._stderr_handle.close()
        connection._stderr_handle = None

    assert captured["env"] == config.child_env

    return str(captured["cwd"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_cwd_provided", "ws_port"),
    [(True, 19091), (False, 19092)],
    ids=["with-task-cwd", "without-task-cwd"],
)
async def test_codex_connection_launches_subprocess_from_control_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    task_cwd_provided: bool,
    ws_port: int,
) -> None:
    control_root = tmp_path / "project"
    control_root.mkdir(parents=True)
    task_cwd = tmp_path / "task" if task_cwd_provided else None
    if task_cwd is not None:
        task_cwd.mkdir(parents=True)

    captured_cwd = await _capture_codex_launch_cwd(
        monkeypatch,
        control_root=control_root,
        task_cwd=task_cwd,
        ws_port=ws_port,
    )

    assert captured_cwd == str(control_root)


@pytest.mark.parametrize(
    "task_cwd_provided",
    [True, False],
    ids=["with-task-cwd", "without-task-cwd"],
)
def test_codex_managed_bootstrap_request_uses_control_root(
    tmp_path: Path,
    task_cwd_provided: bool,
) -> None:
    control_root = tmp_path / "project"
    control_root.mkdir(parents=True)
    task_cwd = tmp_path / "task" if task_cwd_provided else None
    if task_cwd is not None:
        task_cwd.mkdir(parents=True)

    connection = CodexConnection()
    connection._config = ConnectionConfig(
        spawn_id=SpawnId("p-codex-bootstrap-cwd"),
        harness_id=HarnessId.CODEX,
        prompt="hi",
        control_root=control_root,
        task_cwd=task_cwd,
        child_env={},
    )
    method, payload = connection._thread_bootstrap_request(_build_spec())

    assert method == "thread/start"
    assert payload["cwd"] == str(control_root)


def test_codex_rollout_materialization_uses_control_root_when_task_cwd_provided(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    control_root = tmp_path / "project"
    control_root.mkdir(parents=True)
    task_cwd = tmp_path / "task"
    task_cwd.mkdir(parents=True)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    captured: dict[str, object] = {}

    def _fake_find_attachable_rollout_session_id(
        *,
        codex_home: Path,
        project_root: Path,
        session_id: str,
    ) -> str:
        captured["codex_home"] = codex_home
        captured["project_root"] = project_root
        captured["session_id"] = session_id
        return session_id

    monkeypatch.setattr(
        codex_ws,
        "find_attachable_rollout_session_id",
        _fake_find_attachable_rollout_session_id,
    )
    connection = CodexConnection()
    connection._config = ConnectionConfig(
        spawn_id=SpawnId("p-codex-rollout-cwd"),
        harness_id=HarnessId.CODEX,
        prompt="hi",
        control_root=control_root,
        task_cwd=task_cwd,
        child_env={},
    )
    connection._codex_home = codex_home
    connection._thread_id = "thread-1"

    import asyncio

    asyncio.run(connection._wait_for_rollout_materialization(timeout_seconds=0.1))

    assert captured == {
        "codex_home": codex_home,
        "project_root": control_root,
        "session_id": "thread-1",
    }
