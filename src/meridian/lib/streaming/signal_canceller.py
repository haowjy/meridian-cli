"""Shared cancel pipeline for CLI and app-managed spawns."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.process_cleanup import (
    terminate_recorded_spawn_scopes,
    terminate_spawn_scopes,
)
from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.types import SpawnId
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.terminate import terminate_tree_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.liveness import is_process_alive
from meridian.lib.state.spawn.model import APP_LAUNCH_MODE, SpawnOrigin, SpawnRecord
from meridian.lib.state.timestamps import iso_timestamp_to_epoch

if TYPE_CHECKING:
    from meridian.lib.streaming.spawn_manager import SpawnManager

_WAIT_POLL_INTERVAL_SECS = 0.1
logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CancelOutcome:
    status: SpawnStatus
    origin: SpawnOrigin
    exit_code: int
    already_terminal: bool = False
    finalizing: bool = False


class SignalCanceller:
    """Two-lane cancel dispatcher for spawn lifecycle cancellation."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        grace_seconds: float = 5.0,
        manager: SpawnManager | None = None,
    ) -> None:
        self._runtime_root = runtime_root
        self._grace_seconds = grace_seconds
        self._manager = manager

    async def cancel(self, spawn_id: SpawnId) -> CancelOutcome:
        record = spawn_store.get_spawn(self._runtime_root, spawn_id)
        if record is None:
            raise ValueError(f"Spawn '{spawn_id}' not found")

        if _is_terminal(record.status):
            return _outcome_from_record(record, already_terminal=True)

        if record.status == "finalizing":
            terminal = await self._wait_for_terminal(spawn_id)
            if terminal is not None:
                return _outcome_from_record(terminal, already_terminal=True)
            latest = spawn_store.get_spawn(self._runtime_root, spawn_id) or record
            return CancelOutcome(
                status="finalizing",
                origin="cancel",
                exit_code=_exit_code_or_default(latest),
                finalizing=True,
            )

        if record.launch_mode == APP_LAUNCH_MODE:
            return await self._cancel_app_spawn(spawn_id, record)
        return await self._cancel_cli_spawn(spawn_id, record)

    async def _cancel_cli_spawn(
        self,
        spawn_id: SpawnId,
        record: SpawnRecord,
    ) -> CancelOutcome:
        runner_pid, runner_created_at_epoch = self._resolve_runner_process(record)
        if runner_pid is not None:
            with suppress(ProcessLookupError):
                await asyncio.to_thread(
                    terminate_tree_sync,
                    runner_pid,
                    created_at_epoch=runner_created_at_epoch or 0.0,
                    grace_secs=self._grace_seconds,
                    reason="cancel",
                    scope_id=f"{spawn_id}:runner",
                )
            terminal = await self._wait_for_terminal(spawn_id)
            if terminal is not None:
                return _outcome_from_record(terminal)

            latest = spawn_store.get_spawn(self._runtime_root, spawn_id) or record
            await self._cleanup_recorded_spawn_scopes(latest)
            terminal = await self._wait_for_terminal(spawn_id)
            if terminal is not None:
                return _outcome_from_record(terminal)
            latest = spawn_store.get_spawn(self._runtime_root, spawn_id) or latest
            return CancelOutcome(
                status="finalizing",
                origin="cancel",
                exit_code=_exit_code_or_default(latest),
                finalizing=True,
            )

        await self._cleanup_spawn_scopes(record)

        terminal = await self._wait_for_terminal(spawn_id)
        if terminal is not None:
            return _outcome_from_record(terminal)

        latest = spawn_store.get_spawn(self._runtime_root, spawn_id) or record
        return CancelOutcome(
            status="finalizing",
            origin="cancel",
            exit_code=_exit_code_or_default(latest),
            finalizing=True,
        )

    async def _cleanup_recorded_spawn_scopes(self, record: SpawnRecord) -> None:
        await asyncio.to_thread(
            terminate_recorded_spawn_scopes,
            self._runtime_root,
            record,
            reason="cancel",
            grace_seconds=self._grace_seconds,
        )

    async def _cleanup_spawn_scopes(self, record: SpawnRecord) -> None:
        await asyncio.to_thread(
            terminate_spawn_scopes,
            self._runtime_root,
            record,
            reason="cancel",
            grace_seconds=self._grace_seconds,
        )

    async def _cancel_app_spawn(
        self,
        spawn_id: SpawnId,
        record: SpawnRecord,
    ) -> CancelOutcome:
        if self._manager is None:
            return await self._cancel_app_spawn_over_http(spawn_id)

        cancel_exit_code = record.cancel_intent.exit_code if record.cancel_intent else 130
        await self._manager.stop_spawn(
            spawn_id,
            status="cancelled",
            exit_code=cancel_exit_code,
            error="cancelled",
        )
        terminal = await self._wait_for_terminal(spawn_id)
        if terminal is not None:
            return _outcome_from_record(terminal)

        latest = spawn_store.get_spawn(self._runtime_root, spawn_id) or record
        return CancelOutcome(
            status="finalizing",
            origin="cancel",
            exit_code=_exit_code_or_default(latest),
            finalizing=True,
        )

    async def _cancel_app_spawn_over_http(self, spawn_id: SpawnId) -> CancelOutcome:
        aiohttp = import_module("aiohttp")
        timeout = cast("object", aiohttp.ClientTimeout(total=10.0))

        connector: object | None = None
        if IS_WINDOWS:
            port_file = self._runtime_root / "app.port"
            if not port_file.exists():
                raise RuntimeError(f"app port file not found: {port_file}")
            try:
                port = int(port_file.read_text(encoding="utf-8").strip())
            except ValueError as exc:
                raise RuntimeError(f"invalid app port value in {port_file}") from exc
            url = f"http://127.0.0.1:{port}/api/spawns/{spawn_id}/cancel"
        else:
            socket_path = self._runtime_root / "app.sock"
            if not socket_path.exists():
                raise RuntimeError(f"app socket not found: {socket_path}")
            connector = cast("object", aiohttp.UnixConnector(path=str(socket_path)))
            url = f"http://localhost/api/spawns/{spawn_id}/cancel"

        session_kwargs: dict[str, object] = {"timeout": timeout}
        if connector is not None:
            session_kwargs["connector"] = connector

        try:
            async with (
                aiohttp.ClientSession(**session_kwargs) as session,
                session.post(url) as response,
            ):
                status_code = int(response.status)
                raw_body = await response.text()
        except Exception as exc:
            raise RuntimeError(f"cross-process app cancel request failed: {exc}") from exc

        payload = _parse_json_object(raw_body)
        detail = payload.get("detail")
        detail_text = detail if isinstance(detail, str) else ""
        latest = spawn_store.get_spawn(self._runtime_root, spawn_id)

        if status_code == 200:
            status_value = payload.get("status")
            origin_value = payload.get("origin")
            resolved_status = _status_or_default(str(status_value))
            resolved_origin = _origin_or_default(
                cast("SpawnOrigin | None", origin_value if isinstance(origin_value, str) else None)
            )
            if latest is not None and _is_terminal(latest.status):
                return CancelOutcome(
                    status=resolved_status,
                    origin=resolved_origin,
                    exit_code=_exit_code_or_default(latest),
                )
            return CancelOutcome(status=resolved_status, origin=resolved_origin, exit_code=1)

        if status_code == 409:
            if latest is not None and _is_terminal(latest.status):
                return _outcome_from_record(latest, already_terminal=True)
            inferred_status = _status_from_terminal_detail(detail_text) or "failed"
            return CancelOutcome(
                status=_status_or_default(inferred_status),
                origin="cancel",
                exit_code=1,
                already_terminal=True,
            )

        if status_code == 503:
            if latest is not None:
                return CancelOutcome(
                    status="finalizing",
                    origin="cancel",
                    exit_code=_exit_code_or_default(latest),
                    finalizing=True,
                )
            return CancelOutcome(status="finalizing", origin="cancel", exit_code=1, finalizing=True)

        if status_code == 404:
            raise ValueError(f"Spawn '{spawn_id}' not found")

        detail_message = detail_text or "unknown error"
        raise RuntimeError(
            f"cross-process app cancel request failed ({status_code}): {detail_message}"
        )

    def _resolve_runner_process(self, record: SpawnRecord) -> tuple[int | None, float | None]:
        runner_pid = record.runner_pid
        runner_created_at_epoch = record.runner_created_at_epoch or iso_timestamp_to_epoch(
            record.started_at
        )
        if runner_pid is not None and _pid_is_alive(runner_pid, runner_created_at_epoch):
            return runner_pid, runner_created_at_epoch

        return None, None

    async def _wait_for_terminal(self, spawn_id: SpawnId) -> SpawnRecord | None:
        deadline = time.monotonic() + max(0.0, self._grace_seconds)
        while True:
            current = spawn_store.get_spawn(self._runtime_root, spawn_id)
            if current is not None and _is_terminal(current.status):
                return current
            now = time.monotonic()
            if now >= deadline:
                return None
            await asyncio.sleep(min(_WAIT_POLL_INTERVAL_SECS, deadline - now))


def _is_terminal(status: str) -> bool:
    return status in TERMINAL_SPAWN_STATUSES


def _outcome_from_record(
    record: SpawnRecord,
    *,
    already_terminal: bool = False,
) -> CancelOutcome:
    return CancelOutcome(
        status=_status_or_default(record.status),
        origin=_origin_or_default(record.terminal_origin),
        exit_code=_exit_code_or_default(record),
        already_terminal=already_terminal,
    )


def _status_or_default(status: str) -> SpawnStatus:
    if status in {"queued", "running", "finalizing", "succeeded", "failed", "cancelled"}:
        return cast("SpawnStatus", status)
    return "failed"


def _origin_or_default(origin: SpawnOrigin | None) -> SpawnOrigin:
    if origin is None:
        return "cancel"
    return origin


def _exit_code_or_default(record: SpawnRecord) -> int:
    return record.exit_code if record.exit_code is not None else 1


def _pid_is_alive(pid: int | None, started_epoch: float | None) -> bool:
    if pid is None or pid <= 0:
        return False
    return is_process_alive(pid, created_after_epoch=started_epoch)


def _parse_json_object(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return cast("dict[str, object]", payload)


def _status_from_terminal_detail(detail: str) -> str | None:
    prefix = "spawn already terminal:"
    normalized = detail.strip()
    if not normalized.startswith(prefix):
        return None
    status = normalized[len(prefix) :].strip()
    if not status:
        return None
    return status


__all__ = ["CancelOutcome", "SignalCanceller"]
