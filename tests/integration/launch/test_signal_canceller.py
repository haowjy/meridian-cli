from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.spawn.model import LaunchMode, SpawnRecord, TerminalFacts
from meridian.lib.state.spawn.repository import Applied
from meridian.lib.streaming.signal_canceller import SignalCanceller


def _start_spawn(
    runtime_root: Path,
    *,
    spawn_id: str,
    launch_mode: LaunchMode,
    runner_pid: int | None = None,
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


def _make_record(
    *,
    status: str = "running",
    launch_mode: str = "background",
    runner_pid: int | None = 12345,
    worker_pid: int | None = None,
    started_at: str | None = "2024-01-01T00:00:00Z",
    exit_code: int | None = None,
    terminal_origin: str | None = None,
) -> SpawnRecord:
    terminal = (
        TerminalFacts(
            exit_code=exit_code if exit_code is not None else 1,
            finished_at=started_at or "2024-01-01T00:00:00Z",
            published_at=started_at or "2024-01-01T00:00:00Z",
            origin=terminal_origin or "runner",  # type: ignore[arg-type]
        )
        if status in {"succeeded", "failed", "cancelled", "timed_out"}
        else None
    )
    return SpawnRecord(
        id="s-test",
        chat_id=None,
        parent_id=None,
        model=None,
        agent=None,
        agent_path=None,
        skills=(),
        skill_paths=(),
        harness=None,
        kind="child",
        desc=None,
        work_id=None,
        harness_session_id=None,
        execution_cwd=None,
        claude_config_dir=None,
        launch_mode=launch_mode,  # type: ignore[arg-type]
        worker_pid=worker_pid,
        runner_pid=runner_pid,
        runner_created_at_epoch=None,
        status=status,  # type: ignore[arg-type]
        prompt=None,
        started_at=started_at,
        last_attempt_exited_at=None,
        last_attempt_exit_code=None,
        runner_exit=None,
        terminal=terminal,
    )


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
) -> None:
    """Without scope records, cancel falls back to runner tree termination."""
    spawn_id = SpawnId("s-test")
    running_record = _make_record(runner_pid=99, started_at="2024-06-15T12:00:00Z")
    cancelled_record = _make_record(
        status="cancelled",
        runner_pid=99,
        exit_code=130,
        terminal_origin="cancel",
    )

    captured: list[tuple[int, float, str]] = []

    def _capture_tree(pid: int, *, created_at_epoch: float, scope_id: str, **_: object) -> None:
        captured.append((pid, created_at_epoch, scope_id))

    canceller = SignalCanceller(runtime_root=tmp_path, grace_seconds=2.0)

    with (
        patch(
            "meridian.lib.streaming.signal_canceller.spawn_store.get_spawn",
            side_effect=[running_record, cancelled_record],
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.is_process_alive",
            return_value=True,
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
            side_effect=_capture_tree,
        ),
    ):
        outcome = await canceller._cancel_cli_spawn(spawn_id, running_record)

    assert captured and captured[0][0] == 99
    assert captured[0][1] > 0.0
    assert captured[0][2] == "s-test:runner"
    assert outcome.status == "cancelled"
    assert outcome.exit_code == 130


@pytest.mark.asyncio
async def test_cancel_cli_spawn_returns_finalizing_when_terminal_state_never_arrives(
    tmp_path: Path,
) -> None:
    """Cancellation should surface finalizing when durable terminal state does not arrive."""
    spawn_id = SpawnId("s-test")
    running_record = _make_record(runner_pid=777)

    canceller = SignalCanceller(runtime_root=tmp_path, grace_seconds=0.0)

    with (
        patch(
            "meridian.lib.streaming.signal_canceller.spawn_store.get_spawn",
            return_value=running_record,
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.is_process_alive",
            return_value=True,
        ),
        patch("meridian.lib.streaming.signal_canceller.terminate_tree_sync"),
    ):
        outcome = await canceller._cancel_cli_spawn(spawn_id, running_record)

    assert outcome.finalizing is True
    assert outcome.status == "finalizing"


@pytest.mark.asyncio
async def test_cancel_cli_spawn_does_not_run_legacy_worker_fallback_after_runner_signal(
    tmp_path: Path,
) -> None:
    """Post-runner containment cleanup should not unguardedly re-signal legacy worker_pid."""
    spawn_id = SpawnId("s-test")
    running_record = _make_record(runner_pid=777, worker_pid=888)

    canceller = SignalCanceller(runtime_root=tmp_path, grace_seconds=0.0)

    with (
        patch(
            "meridian.lib.streaming.signal_canceller.spawn_store.get_spawn",
            return_value=running_record,
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.is_process_alive",
            return_value=True,
        ),
        patch("meridian.lib.streaming.signal_canceller.terminate_tree_sync"),
        patch(
            "meridian.lib.core.process_cleanup.read_scopes_from_disk",
            return_value=[],
        ),
        patch(
            "meridian.lib.core.process_cleanup.terminate_tree_sync",
            side_effect=AssertionError("legacy worker fallback should not run"),
        ),
    ):
        outcome = await canceller._cancel_cli_spawn(spawn_id, running_record)

    assert outcome.finalizing is True
    assert outcome.status == "finalizing"


@pytest.mark.asyncio
async def test_cleanup_spawn_scopes_uses_release_id_for_duplicate_labels(
    tmp_path: Path,
) -> None:
    first_backend = _make_scope("backend", root_pid=101)
    second_backend = _make_scope("backend", root_pid=202)
    session_backend = _make_scope(
        "backend",
        root_pid=303,
        owner_policy="session_owned",
    )
    terminated_release_ids: list[str] = []
    marked_release_ids: list[str] = []

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

    canceller = SignalCanceller(runtime_root=tmp_path, grace_seconds=0.0)

    with (
        patch(
            "meridian.lib.core.process_cleanup.read_scopes_from_disk",
            return_value=[first_backend, second_backend, session_backend],
        ),
        patch(
            "meridian.lib.core.process_cleanup.is_scope_released",
            side_effect=lambda _root, _sid, release_id: release_id
            == first_backend.release_id,
        ),
        patch(
            "meridian.lib.core.process_cleanup.psutil.Process",
            return_value=type("LiveProc", (), {"create_time": lambda self: 1_700_000_000.0})(),
        ),
        patch(
            "meridian.lib.core.process_cleanup.terminate_scope_sync",
            side_effect=_terminate_scope,
        ),
        patch(
            "meridian.lib.core.process_cleanup.mark_scope_released",
            side_effect=lambda _root, _sid, release_id: marked_release_ids.append(
                release_id
            ),
        ),
    ):
        await canceller._cleanup_spawn_scopes(_make_record())

    assert first_backend.scope_id == second_backend.scope_id
    assert terminated_release_ids == [second_backend.release_id]
    assert marked_release_ids == [second_backend.release_id]
