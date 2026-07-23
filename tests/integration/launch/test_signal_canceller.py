from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.process_scope_projection import (
    is_scope_released,
    mark_scope_released,
    record_scope,
)
from meridian.lib.state.spawn.model import LaunchMode
from meridian.lib.state.spawn.repository import Applied
from meridian.lib.streaming.signal_canceller import SignalCanceller


def _start_spawn(
    runtime_root: Path,
    *,
    spawn_id: str,
    launch_mode: LaunchMode,
    runner_pid: int | None = None,
    worker_pid: int | None = None,
    started_at: str | None = None,
) -> str:
    return str(
        spawn_store.start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
            spawn_id=spawn_id,
            launch_mode=launch_mode,
            runner_pid=runner_pid,
            worker_pid=worker_pid,
            started_at=started_at,
        )
    )


@pytest.mark.asyncio
async def test_signal_canceller_returns_idempotent_outcome_for_terminal_spawn(
    tmp_path: Path,
) -> None:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(runtime_root, spawn_id="p1", launch_mode="foreground")
    spawn_store.finalize_spawn(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=1,
        origin="runner",
        error="boom",
    )

    outcome = await SignalCanceller(
        runtime_root=runtime_root,
    ).cancel(SpawnId(spawn_id))

    assert outcome.already_terminal is True
    assert outcome.status == "failed"
    assert outcome.origin == "runner"
    assert outcome.exit_code == 1


@pytest.mark.asyncio
async def test_signal_canceller_finalizing_gate_skips_sigterm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(runtime_root, spawn_id="p1", launch_mode="foreground", runner_pid=4321)
    assert isinstance(spawn_store.mark_finalizing(runtime_root, spawn_id), Applied)

    def _unexpected_terminate(pid: int, **_kwargs: object) -> None:
        raise AssertionError(f"terminate_tree_sync must not run for finalizing rows: pid={pid}")

    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
        _unexpected_terminate,
    )
    outcome = await SignalCanceller(
        runtime_root=runtime_root,
        grace_seconds=0.01,
    ).cancel(SpawnId(spawn_id))

    assert outcome.status == "finalizing"
    assert outcome.finalizing is True


@pytest.mark.asyncio
async def test_signal_canceller_cli_lane_sends_sigterm_and_returns_terminal_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(runtime_root, spawn_id="p1", launch_mode="foreground", runner_pid=7654)

    def fake_is_process_alive(pid: int, created_after_epoch: float | None = None) -> bool:
        _ = created_after_epoch
        return pid == 7654

    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.is_process_alive",
        fake_is_process_alive,
    )
    terminated_pids: list[int] = []

    def _fake_terminate_tree_sync(
        pid: int,
        *,
        created_at_epoch: float = 0.0,
        grace_secs: float = 5.0,
        reason: str = "cancel",
        scope_id: str = "",
        degraded_fallback: bool = False,
    ) -> None:
        terminated_pids.append(pid)
        spawn_store.finalize_spawn(
            runtime_root,
            spawn_id,
            status="cancelled",
            exit_code=143,
            origin="runner",
            error="cancelled",
        )

    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
        _fake_terminate_tree_sync,
    )
    outcome = await SignalCanceller(
        runtime_root=runtime_root,
    ).cancel(SpawnId(spawn_id))

    assert terminated_pids == [7654]
    assert outcome.status == "cancelled"
    assert outcome.origin == "runner"
    assert outcome.exit_code == 143
    assert outcome.finalizing is False


@pytest.mark.asyncio
async def test_signal_canceller_app_lane_uses_manager_stop_spawn(
    tmp_path: Path,
) -> None:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(runtime_root, spawn_id="p1", launch_mode="app", runner_pid=3456)
    calls: list[tuple[str, str, int, str | None]] = []

    class _FakeManager:
        async def stop_spawn(
            self,
            target_spawn_id: SpawnId,
            *,
            status: str = "cancelled",
            exit_code: int = 1,
            error: str | None = None,
        ) -> None:
            calls.append((str(target_spawn_id), status, exit_code, error))
            spawn_store.finalize_spawn(
                runtime_root,
                target_spawn_id,
                status="cancelled",
                exit_code=143,
                origin="runner",
                error="cancelled",
            )

    outcome = await SignalCanceller(
        runtime_root=runtime_root,
        manager=cast("Any", _FakeManager()),
    ).cancel(SpawnId(spawn_id))

    assert calls == [(spawn_id, "cancelled", 130, "cancelled")]
    assert outcome.status == "cancelled"
    assert outcome.origin == "runner"
@pytest.mark.asyncio
async def test_signal_canceller_runnerless_cancel_preserves_running_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SignalCanceller does not claim terminal authority without a runner."""

    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(runtime_root, spawn_id="p1", launch_mode="foreground", runner_pid=None)
    outcome = await SignalCanceller(
        runtime_root=runtime_root,
    ).cancel(SpawnId(spawn_id))

    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert outcome.status == "finalizing"
    assert outcome.finalizing is True
    assert row is not None
    assert row.status == "running"


async def _start_http_socket_server(
    socket_path: Path,
    *,
    status_code: int,
    body: dict[str, object],
    on_request: Callable[[], None] | None = None,
) -> asyncio.AbstractServer:
    socket_path.unlink(missing_ok=True)

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with suppress(Exception):
            while True:
                line = await reader.readline()
                if not line or line == b"\r\n":
                    break
        if on_request is not None:
            on_request()
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        status_text = {
            200: "OK",
            404: "Not Found",
            409: "Conflict",
            503: "Service Unavailable",
        }.get(status_code, "Error")
        writer.write(
            (
                f"HTTP/1.1 {status_code} {status_text}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode()
            + payload
        )
        with suppress(Exception):
            await writer.drain()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()

    if IS_WINDOWS:
        server = await asyncio.start_server(_handler, host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        port_file = socket_path.parent / "app.port"
        port_file.write_text(str(port))
        return server
    return await asyncio.start_unix_server(_handler, path=str(socket_path))


@pytest.mark.asyncio
async def test_signal_canceller_app_lane_cross_process_http_success(
    tmp_path: Path,
) -> None:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(runtime_root, spawn_id="p1", launch_mode="app")
    socket_path = runtime_root / "app.sock"

    def _finalize_spawn() -> None:
        spawn_store.finalize_spawn(
            runtime_root,
            spawn_id,
            status="cancelled",
            exit_code=143,
            origin="runner",
            error="cancelled",
        )

    server = await _start_http_socket_server(
        socket_path,
        status_code=200,
        body={"ok": True, "status": "cancelled", "origin": "runner"},
        on_request=_finalize_spawn,
    )
    try:
        outcome = await SignalCanceller(runtime_root=runtime_root).cancel(SpawnId(spawn_id))
    finally:
        server.close()
        await server.wait_closed()

    assert outcome.status == "cancelled"
    assert outcome.origin == "runner"
    assert outcome.exit_code == 143
    assert outcome.finalizing is False


@pytest.mark.asyncio
async def test_signal_canceller_app_lane_cross_process_http_409_maps_already_terminal(
    tmp_path: Path,
) -> None:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(runtime_root, spawn_id="p1", launch_mode="app")
    socket_path = runtime_root / "app.sock"

    server = await _start_http_socket_server(
        socket_path,
        status_code=409,
        body={"detail": "spawn already terminal: failed"},
    )
    try:
        outcome = await SignalCanceller(runtime_root=runtime_root).cancel(SpawnId(spawn_id))
    finally:
        server.close()
        await server.wait_closed()

    assert outcome.already_terminal is True
    assert outcome.status == "failed"
    assert outcome.origin == "cancel"
    assert outcome.finalizing is False


@pytest.mark.asyncio
async def test_signal_canceller_app_lane_cross_process_http_503_maps_finalizing(
    tmp_path: Path,
) -> None:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(runtime_root, spawn_id="p1", launch_mode="app")
    socket_path = runtime_root / "app.sock"

    server = await _start_http_socket_server(
        socket_path,
        status_code=503,
        body={"detail": "spawn is finalizing"},
    )
    try:
        outcome = await SignalCanceller(runtime_root=runtime_root).cancel(SpawnId(spawn_id))
    finally:
        server.close()
        await server.wait_closed()

    assert outcome.finalizing is True
    assert outcome.status == "finalizing"
    assert outcome.origin == "cancel"


def _make_scope(
    scope_id: str,
    *,
    root_pid: int,
    owner_policy: str = "spawn_owned",
) -> ProcessScopeSnapshot:
    return ProcessScopeSnapshot(
        scope_id=scope_id,
        owner_policy=owner_policy,
        owner_id="s-test",
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=root_pid,
        root_created_at_epoch=1_700_000_000.0,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )


@pytest.mark.asyncio
async def test_cancel_cli_spawn_legacy_path_uses_tree_termination_with_started_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without scope records, cancel falls back to runner tree termination."""
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(
        runtime_root,
        spawn_id="s-test",
        launch_mode="background",
        runner_pid=99,
        started_at="2024-06-15T12:00:00Z",
    )
    captured: list[tuple[int, float, str]] = []

    def _capture_tree(pid: int, *, created_at_epoch: float, scope_id: str, **_: object) -> None:
        captured.append((pid, created_at_epoch, scope_id))
        spawn_store.finalize_spawn(
            runtime_root,
            spawn_id,
            status="cancelled",
            exit_code=130,
            origin="cancel",
        )

    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.is_process_alive",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
        _capture_tree,
    )

    outcome = await SignalCanceller(
        runtime_root=runtime_root, grace_seconds=2.0
    ).cancel(SpawnId(spawn_id))

    assert captured and captured[0][0] == 99
    assert captured[0][1] > 0.0
    assert captured[0][2] == f"{spawn_id}:runner"
    assert outcome.status == "cancelled"
    assert outcome.exit_code == 130


@pytest.mark.asyncio
async def test_cancel_cli_spawn_returns_finalizing_when_terminal_state_never_arrives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation should surface finalizing when durable terminal state does not arrive."""
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(
        runtime_root,
        spawn_id="s-test",
        launch_mode="background",
        runner_pid=777,
    )
    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.is_process_alive",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
        lambda *_args, **_kwargs: None,
    )

    outcome = await SignalCanceller(
        runtime_root=runtime_root, grace_seconds=0.0
    ).cancel(SpawnId(spawn_id))

    assert outcome.finalizing is True
    assert outcome.status == "finalizing"
    persisted = spawn_store.get_spawn(runtime_root, spawn_id)
    assert persisted is not None
    assert persisted.status == "running"


@pytest.mark.asyncio
async def test_cancel_cli_spawn_does_not_run_legacy_worker_fallback_after_runner_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-runner containment cleanup should not unguardedly re-signal legacy worker_pid."""
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(
        runtime_root,
        spawn_id="s-test",
        launch_mode="background",
        runner_pid=777,
        worker_pid=888,
    )
    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.is_process_alive",
        lambda *_args, **_kwargs: True,
    )
    runner_signals: list[int] = []
    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
        lambda pid, **_kwargs: runner_signals.append(pid),
    )

    def _unexpected_worker_signal(**_kwargs: object) -> None:
        raise AssertionError("legacy worker fallback should not run")

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_tree_sync",
        _unexpected_worker_signal,
    )

    outcome = await SignalCanceller(
        runtime_root=runtime_root, grace_seconds=0.0
    ).cancel(SpawnId(spawn_id))

    assert runner_signals == [777]
    assert outcome.finalizing is True
    assert outcome.status == "finalizing"


@pytest.mark.asyncio
async def test_cleanup_spawn_scopes_uses_release_id_for_duplicate_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(
        runtime_root,
        spawn_id="s-test",
        launch_mode="background",
        runner_pid=None,
    )
    first_backend = _make_scope("backend", root_pid=101)
    second_backend = _make_scope("backend", root_pid=202)
    session_backend = _make_scope(
        "backend",
        root_pid=303,
        owner_policy="session_owned",
    )
    for scope in (first_backend, second_backend, session_backend):
        record_scope(runtime_root, SpawnId(spawn_id), scope)
    mark_scope_released(runtime_root, SpawnId(spawn_id), first_backend.release_id)
    terminated_release_ids: list[str] = []

    def _terminate_scope(scope: ProcessScopeSnapshot, **kwargs: object) -> CleanupResult:
        terminated_release_ids.append(scope.release_id)
        return CleanupResult(
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            descendant_count=0,
            reason=str(kwargs["reason"]),
            grace_seconds=0.0,
            kill_escalated=False,
            degraded_fallback=False,
            skip_reason=None,
        )

    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.psutil.Process",
        lambda _pid: type(
            "LiveProc", (), {"create_time": lambda self: 1_700_000_000.0}
        )(),
    )
    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
        _terminate_scope,
    )

    outcome = await SignalCanceller(
        runtime_root=runtime_root, grace_seconds=0.0
    ).cancel(SpawnId(spawn_id))

    assert first_backend.scope_id == second_backend.scope_id
    assert terminated_release_ids == [second_backend.release_id]
    assert is_scope_released(
        runtime_root, SpawnId(spawn_id), second_backend.release_id
    )
    assert not is_scope_released(
        runtime_root, SpawnId(spawn_id), session_backend.release_id
    )
    assert outcome.status == "finalizing"
