"""Pi subprocess-backed connection.

Phase 1/2 uses Pi print mode (``meridian-pi -p --mode json``) rather than RPC.
The connection still implements Meridian's streaming connection contract so the
spawn runner can durably drain Pi JSONL events through the same path as other
harnesses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from asyncio.subprocess import PIPE, Process
from collections.abc import AsyncIterator
from io import BufferedWriter
from typing import Final, cast

from meridian.lib.core.telemetry import StartupPhase, StartupPhaseEmitter
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.bundle import project_subprocess_spec
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionNotReady,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    validate_prompt_size,
)
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.harness.semantics import clears_signal
from meridian.lib.launch.constants import BASE_COMMAND_PI_SUBPROCESS
from meridian.lib.launch.env import inherit_child_env
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.observability.trace_helpers import (
    trace_parse_error,
    trace_state_change,
    trace_wire_recv,
    trace_wire_send,
)
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.state.paths import resolve_spawn_log_dir

logger = logging.getLogger(__name__)
_HARNESS_NAME: Final = HarnessId.PI.value
_STDOUT_READLINE_LIMIT: Final[int] = 10 * 1024 * 1024
_PROCESS_KILL_GRACE_SECONDS: Final[float] = 5.0
_BLOCKED_CHILD_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        "MERIDIAN_ACTIVE_WORK_ID",
        "MERIDIAN_ACTIVE_WORK_DIR",
    }
)


class PiConnection(HarnessConnection[ResolvedLaunchSpec]):
    """Drain Pi print-mode JSONL through Meridian's streaming runner."""

    _CAPABILITIES = ConnectionCapabilities(
        mid_turn_injection="queue",
        supports_steer=False,
        supports_cancel=True,
        runtime_model_switch=False,
        structured_reasoning=True,
        supports_primary_observer=False,
        supports_runtime_hitl=False,
        supported_startup_phases=frozenset(
            phase.value
            for phase in (
                StartupPhase.WAITING_FOR_CONNECTION,
                StartupPhase.HARNESS_READY,
            )
        ),
    )
    _ALLOWED_TRANSITIONS: Final[dict[ConnectionState, set[ConnectionState]]] = {
        "created": {"starting", "stopping", "stopped", "failed"},
        "starting": {"connected", "stopping", "stopped", "failed"},
        "connected": {"stopping", "failed"},
        "stopping": {"stopped", "failed"},
        "failed": {"stopped"},
        "stopped": set(),
    }

    def __init__(self) -> None:
        self._state: ConnectionState = "created"
        self._spawn_id: SpawnId = SpawnId("")
        self._process: Process | None = None
        self._stderr_handle: BufferedWriter | None = None
        self._event_stream_started = False
        self._session_id: str | None = None
        self._tracer = None
        self._startup_emitter: StartupPhaseEmitter | None = None

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.PI

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return self._CAPABILITIES

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def subprocess_pid(self) -> int | None:
        process = self._process
        if process is None:
            return None
        return process.pid

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        if self._state != "created":
            raise RuntimeError(f"Connection can only start from 'created', got '{self._state}'")

        validate_prompt_size(config)
        self._spawn_id = config.spawn_id
        self._tracer = config.debug_tracer
        self._startup_emitter = StartupPhaseEmitter(
            str(config.spawn_id),
            harness_id=config.harness_id.value,
            model=spec.model,
            agent=spec.agent_name,
        )
        self._set_state("starting")

        try:
            await self._start_subprocess(config, spec)
            self._emit_startup_phase(StartupPhase.WAITING_FOR_CONNECTION)
            self._set_state("connected")
        except Exception:
            self._mark_failed("Pi connection startup failed.")
            await self._cleanup_resources(terminate_process=True)
            raise

    async def stop(self) -> None:
        if self._state == "stopped":
            return
        if self._state not in {"stopping", "failed"}:
            self._set_state("stopping")
        await self._cleanup_resources(terminate_process=True)
        self._set_state("stopped")

    def health(self) -> bool:
        return self._state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text
        raise ConnectionNotReady("Pi Phase 1/2 subprocess mode does not support injection.")

    async def send_cancel(self) -> None:
        if self._state in {"stopping", "stopped", "failed"}:
            return
        self._set_state("stopping")
        await self._signal_process(signal.SIGINT)

    async def events(self) -> AsyncIterator[HarnessEvent]:
        process = self._process
        if process is None or process.stdout is None:
            return
        if self._event_stream_started:
            raise RuntimeError("events() iterator already consumed")
        self._event_stream_started = True

        try:
            while True:
                try:
                    line_bytes = await process.stdout.readline()
                except Exception as exc:
                    if self._state not in {"stopping", "stopped"}:
                        detail = f"Failed to read Pi stdout: {exc}"
                        self._mark_failed(detail)
                        yield self._error_event(detail)
                    return

                if not line_bytes:
                    return_code = process.returncode
                    if return_code is None:
                        return_code = await process.wait()
                    if return_code != 0 and self._state not in {"stopping", "stopped"}:
                        detail = f"Pi subprocess exited with code {return_code}."
                        self._mark_failed(detail)
                        yield self._error_event(detail)
                    return

                raw_text = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
                if not raw_text.strip():
                    continue

                trace_wire_recv(self._tracer, "stdout_line", raw_text, bytes=len(line_bytes))
                event = self._parse_stdout_line(raw_text)
                if event is None:
                    continue
                if event.event_type == "session":
                    session_id = event.payload.get("id")
                    if isinstance(session_id, str) and session_id.strip():
                        self._session_id = session_id.strip()
                    self._emit_startup_phase(StartupPhase.HARNESS_READY)
                if clears_signal(event):
                    self._set_state("stopping")
                yield event
        finally:
            await self._cleanup_resources(terminate_process=False)

    async def _start_subprocess(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        spawn_dir = resolve_spawn_log_dir(config.control_root, config.spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)

        stderr_path = spawn_dir / "stderr.log"
        self._stderr_handle = stderr_path.open("ab")

        command = project_subprocess_spec(
            self.harness_id,
            spec,
            base_command=BASE_COMMAND_PI_SUBPROCESS,
        )
        env = inherit_child_env(
            os.environ,
            config.env_overrides,
            blocked=_BLOCKED_CHILD_ENV_VARS,
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(config.task_cwd or config.control_root),
                env=env,
                stdin=PIPE,
                stdout=PIPE,
                stderr=self._stderr_handle,
                limit=_STDOUT_READLINE_LIMIT,
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HarnessBinaryNotFound.from_os_error(
                harness_id=self.harness_id,
                error=exc,
                binary_name=command[0],
            ) from exc

    def _parse_stdout_line(self, line: str) -> HarnessEvent | None:
        payload_text = line.strip()
        if not payload_text:
            return None
        try:
            payload_obj = json.loads(payload_text)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed Pi stdout line: %s", payload_text)
            trace_parse_error(self._tracer, "pi", payload_text, error="malformed_json")
            return None
        if not isinstance(payload_obj, dict):
            logger.warning("Skipping non-object Pi stdout line: %s", payload_text)
            trace_parse_error(self._tracer, "pi", payload_text, error="non_object")
            return None

        payload = cast("dict[str, object]", payload_obj)
        event_type = payload.get("type")
        if not isinstance(event_type, str) or not event_type.strip():
            logger.warning("Skipping Pi stdout line without string 'type': %s", payload_text)
            trace_parse_error(self._tracer, "pi", payload_text, error="missing_type")
            return None

        return HarnessEvent(
            event_type=event_type,
            payload=payload,
            harness_id=_HARNESS_NAME,
            raw_text=line,
        )

    def _set_state(self, next_state: ConnectionState) -> None:
        if next_state == self._state:
            return
        allowed = self._ALLOWED_TRANSITIONS[self._state]
        if next_state not in allowed:
            raise RuntimeError(
                f"Invalid connection state transition: {self._state} -> {next_state}"
            )
        trace_state_change(self._tracer, "pi", self._state, next_state)
        self._state = next_state

    def _mark_failed(self, reason: str) -> None:
        if self._state not in {"failed", "stopped"}:
            try:
                self._set_state("failed")
            except RuntimeError:
                logger.exception("Failed to transition Pi connection into failed state")
        logger.warning("Pi connection failed: %s", reason)

    def _error_event(self, message: str) -> HarnessEvent:
        return HarnessEvent(
            event_type="error/connectionClosed",
            payload={"type": "error/connectionClosed", "message": message},
            harness_id=_HARNESS_NAME,
        )

    async def _signal_process(self, sig: signal.Signals) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        trace_wire_send(self._tracer, "signal_sent", "", signal=sig.name)
        if IS_WINDOWS:
            process.terminate()
        else:
            process.send_signal(sig)

    async def _cleanup_resources(self, *, terminate_process: bool) -> None:
        if terminate_process:
            await self._terminate_process()
        self._close_log_handles()

    async def _terminate_process(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except Exception:
                    logger.debug("Failed to close Pi subprocess stdin", exc_info=True)
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_PROCESS_KILL_GRACE_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()
        self._process = None

    def _close_log_handles(self) -> None:
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None

    def _emit_startup_phase(self, phase: StartupPhase) -> None:
        emitter = self._startup_emitter
        if emitter is not None:
            emitter.emit(phase)


__all__ = ["PiConnection"]
