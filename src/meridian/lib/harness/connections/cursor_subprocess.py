"""Cursor subprocess transport connection over stdout NDJSON."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from asyncio.subprocess import DEVNULL, PIPE, Process
from collections.abc import AsyncIterator
from contextlib import suppress
from io import BufferedWriter
from typing import Final, cast

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.bundle import project_subprocess_spec
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.launch.constants import BASE_COMMAND_CURSOR_SUBPROCESS, BLOCKED_CHILD_ENV_VARS
from meridian.lib.launch.env import inherit_child_env
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state.paths import resolve_spawn_log_dir

logger = logging.getLogger(__name__)

_HARNESS_NAME: Final[str] = HarnessId.CURSOR.value
_PROCESS_STOP_TIMEOUT_SECONDS: Final[float] = 10.0


class CursorSubprocessConnection(HarnessConnection[ResolvedLaunchSpec]):
    """Subprocess Cursor harness connection over stdout NDJSON."""

    _CAPABILITIES = ConnectionCapabilities(
        mid_turn_injection="queue",
        supports_steer=False,
        supports_cancel=True,
        runtime_model_switch=False,
        structured_reasoning=False,
        supports_primary_observer=False,
        supports_runtime_hitl=False,
    )

    def __init__(self) -> None:
        self._state: ConnectionState = "created"
        self._spawn_id: SpawnId = SpawnId("")
        self._process: Process | None = None
        self._stderr_handle: BufferedWriter | None = None
        self._event_stream_started = False
        self._protocol_validated = False
        self._stop_lock = asyncio.Lock()

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CURSOR

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return self._CAPABILITIES

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def subprocess_pid(self) -> int | None:
        process = self._process
        if process is None:
            return None
        return process.pid

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        if self._state != "created":
            raise RuntimeError(f"Cannot start Cursor connection from state '{self._state}'")

        self._spawn_id = config.spawn_id
        self._set_state("starting")

        spawn_dir = resolve_spawn_log_dir(config.control_root, config.spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_handle = (spawn_dir / "stderr.log").open("ab")

        command = project_subprocess_spec(
            self.harness_id,
            spec,
            base_command=BASE_COMMAND_CURSOR_SUBPROCESS,
        )
        env = inherit_child_env(
            os.environ,
            config.env_overrides,
            blocked=BLOCKED_CHILD_ENV_VARS,
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(config.control_root),
                env=env,
                stdin=DEVNULL,
                stdout=PIPE,
                stderr=self._stderr_handle,
            )
            self._set_state("connected")
        except Exception:
            self._set_state("failed")
            await self._cleanup_resources(terminate_process=True)
            raise

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        async with self._stop_lock:
            if self._state == "stopped":
                return StopResult()
            if self._state not in {"stopping", "failed"}:
                self._set_state("stopping")
            await self._cleanup_resources(terminate_process=True)
            self._set_state("stopped")
            return StopResult()

    def health(self) -> bool:
        return self._state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text
        raise RuntimeError(
            "Cursor subprocess transport does not support live user-message injection"
        )

    async def send_cancel(self) -> None:
        if self._state == "stopped":
            return
        if self._state != "failed":
            self._set_state("stopping")
        await self._terminate_process()

    async def events(self) -> AsyncIterator[HarnessEvent]:
        process = self._process
        if process is None:
            return
        stdout = process.stdout
        if stdout is None:
            return
        if self._event_stream_started:
            raise RuntimeError("events() iterator already consumed")
        self._event_stream_started = True

        while True:
            try:
                line_bytes = await stdout.readline()
            except Exception as exc:
                if self._state not in {"stopping", "stopped"}:
                    detail = f"Failed to read Cursor stdout: {exc}"
                    self._set_state("failed")
                    yield self._error_event(detail)
                return

            if not line_bytes:
                return_code = process.returncode
                if return_code is None:
                    return_code = await process.wait()
                if return_code != 0 and self._state not in {"stopping", "stopped"}:
                    detail = f"Cursor subprocess exited with code {return_code}."
                    self._set_state("failed")
                    yield self._error_event(detail)
                return

            raw_text = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            if not raw_text.strip():
                continue
            event = self._parse_stdout_line(raw_text)
            if event is None:
                if self._protocol_validated:
                    continue
                detail = (
                    "Cursor protocol mismatch: expected NDJSON object with non-empty string "
                    f"'type', got: {raw_text.strip()!r}"
                )
                self._set_state("failed")
                yield self._error_event(detail, raw_text=raw_text)
                return

            self._protocol_validated = True
            yield event

    def _parse_stdout_line(self, raw_text: str) -> HarnessEvent | None:
        payload_text = raw_text.strip()
        if not payload_text:
            return None
        try:
            payload_obj = json.loads(payload_text)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed Cursor stdout line: %s", payload_text)
            return None
        if not isinstance(payload_obj, dict):
            logger.warning("Skipping non-object Cursor stdout line: %s", payload_text)
            return None

        payload = cast("dict[str, object]", payload_obj)
        raw_event_type = payload.get("type")
        if not isinstance(raw_event_type, str) or not raw_event_type.strip():
            logger.warning(
                "Skipping Cursor stdout line without string 'type': %s",
                payload_text,
            )
            return None

        return HarnessEvent(
            event_type=raw_event_type,
            payload=payload,
            harness_id=_HARNESS_NAME,
            raw_text=raw_text,
        )

    def _set_state(self, next_state: ConnectionState) -> None:
        if self._state == next_state:
            return
        self._state = next_state

    async def _cleanup_resources(self, *, terminate_process: bool) -> None:
        if terminate_process:
            await self._terminate_process()
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None

    async def _terminate_process(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        self._process = None

    def _error_event(self, message: str, *, raw_text: str | None = None) -> HarnessEvent:
        return HarnessEvent(
            event_type="error/connectionClosed",
            payload={"type": "error", "message": message},
            harness_id=_HARNESS_NAME,
            raw_text=raw_text,
        )


__all__ = ["CursorSubprocessConnection"]
