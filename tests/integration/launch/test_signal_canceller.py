from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.core.types import SpawnId
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.spawn.model import LaunchMode
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


def _complete_spawn_for_runtime(runtime_root: Path) -> Callable[..., Any]:
    return SpawnApplicationService(
        runtime_root,
        SpawnLifecycleService(runtime_root),
    ).complete_spawn


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
        complete_spawn=_complete_spawn_for_runtime(runtime_root),
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
    assert spawn_store.mark_finalizing(runtime_root, spawn_id) is True

    def _unexpected_terminate(pid: int, **_kwargs: object) -> None:
        raise AssertionError(f"terminate_tree_sync must not run for finalizing rows: pid={pid}")

    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
        _unexpected_terminate,
    )
    outcome = await SignalCanceller(
        runtime_root=runtime_root,
        grace_seconds=0.01,
        complete_spawn=_complete_spawn_for_runtime(runtime_root),
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
        complete_spawn=_complete_spawn_for_runtime(runtime_root),
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

    assert calls == [(spawn_id, "cancelled", 143, "cancelled")]
    assert outcome.status == "cancelled"
    assert outcome.origin == "runner"


@pytest.mark.asyncio
async def test_signal_canceller_cli_lane_finalizes_when_runner_pid_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(runtime_root, spawn_id="p1", launch_mode="foreground", runner_pid=None)

    def fake_is_process_alive(pid: int, created_after_epoch: float | None = None) -> bool:
        _ = pid, created_after_epoch
        return False

    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.is_process_alive",
        fake_is_process_alive,
    )
    outcome = await SignalCanceller(
        runtime_root=runtime_root,
        complete_spawn=_complete_spawn_for_runtime(runtime_root),
    ).cancel(SpawnId(spawn_id))

    assert outcome.status == "cancelled"
    assert outcome.origin == "cancel"
    assert outcome.exit_code == 130


@pytest.mark.asyncio
async def test_signal_canceller_runnerless_cancel_warns_and_falls_back_when_snapshot_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EARS-CR4.3: cancel warns and falls back to the persisted terminal row."""

    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(runtime_root, spawn_id="p1", launch_mode="foreground", runner_pid=None)
    warnings: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_complete_spawn(*args: object, **kwargs: object) -> Any:
        _ = args, kwargs
        spawn_store.finalize_spawn(
            runtime_root,
            spawn_id,
            status="failed",
            exit_code=91,
            origin="runner",
            error="runner finished first",
        )
        return SimpleNamespace(
            snapshot=None,
            transitioned=False,
            wrote=False,
        )

    def capture_warning(*args: object, **kwargs: object) -> None:
        warnings.append((args, kwargs))

    monkeypatch.setattr(
        "meridian.lib.streaming.signal_canceller.logger.warning",
        capture_warning,
    )
    outcome = await SignalCanceller(
        runtime_root=runtime_root,
        complete_spawn=fake_complete_spawn,
    ).cancel(SpawnId(spawn_id))

    assert outcome.already_terminal is True
    assert outcome.status == "failed"
    assert outcome.origin == "runner"
    assert outcome.exit_code == 91
    assert len(warnings) == 1
    assert warnings[0][0] == ("cancel_race_snapshot_missing",)
    assert warnings[0][1]["spawn_id"] == spawn_id
    assert warnings[0][1]["wrote"] is False


@pytest.mark.asyncio
async def test_signal_canceller_runnerless_cancel_returns_authoritative_winner_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EARS-CR4.1 / EARS-CR4.2: cancel returns the winning authoritative snapshot."""

    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    spawn_id = _start_spawn(runtime_root, spawn_id="p1", launch_mode="foreground", runner_pid=None)
    original_mark_finalizing = SpawnLifecycleService.mark_finalizing

    def race_mark_finalizing(self: SpawnLifecycleService, target_spawn_id: str) -> bool:
        transitioned = original_mark_finalizing(self, target_spawn_id)
        if transitioned:
            spawn_store.finalize_spawn(
                runtime_root,
                target_spawn_id,
                status="failed",
                exit_code=42,
                origin="runner",
                error="runner won race",
            )
        return transitioned

    monkeypatch.setattr(SpawnLifecycleService, "mark_finalizing", race_mark_finalizing)

    outcome = await SignalCanceller(
        runtime_root=runtime_root,
        complete_spawn=_complete_spawn_for_runtime(runtime_root),
    ).cancel(SpawnId(spawn_id))

    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert outcome.already_terminal is True
    assert outcome.status == "failed"
    assert outcome.origin == "runner"
    assert outcome.exit_code == 42
    assert row is not None
    assert row.status == "failed"
    assert row.terminal_origin == "runner"
    assert row.exit_code == 42


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
