"""Pi RPC stdio connection implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import time
from asyncio.subprocess import PIPE, Process
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from io import BufferedWriter
from pathlib import Path
from typing import Final, Literal, cast

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
    StopProgressCallback,
    StopResult,
    validate_prompt_size,
)
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.harness.pi_lifecycle_events import (
    PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES,
    PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION,
    has_unsupported_pi_lifecycle_schema_version,
    redact_pi_command_for_history,
)
from meridian.lib.harness.pi_paths import pi_agent_dir_env_override
from meridian.lib.harness.pi_runtime_resolver import (
    PiRuntimeResolutionError,
    resolve_pi_runtime,
)
from meridian.lib.launch.constants import BASE_COMMAND_PI_SUBPROCESS, BLOCKED_CHILD_ENV_VARS
from meridian.lib.launch.env import inherit_child_env
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.report import compact_pi_failure_output
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
_PROCESS_ABORT_GRACE_SECONDS: Final[float] = 5.0
_PROCESS_KILL_GRACE_SECONDS: Final[float] = 5.0
_PARSE_ERROR_RAW_LINE_LIMIT: Final[int] = 2048
_STDERR_SNIPPET_LIMIT: Final[int] = 4096
_PI_PHASE_EVENT_TYPE: Final[str] = "meridian.pi.lifecycle.phase"
_PI_FIRST_EVENT_TIMEOUT_REASON: Final[str] = "pi_rpc_no_response_after_initial_prompt"
_PI_SPAWNED_PROMPT_REQUIRED_REASON: Final[str] = "pi_rpc_spawned_prompt_required"
_FIRST_STDOUT_AFTER_INITIAL_PROMPT_TIMEOUT_SECONDS: Final[float] = 30.0
_BLOCKED_CHILD_ENV_VARS: Final[frozenset[str]] = BLOCKED_CHILD_ENV_VARS
_PI_SESSION_DIR_FLAG: Final[str] = "--session-dir"
PI_SUBPROCESS_EXIT_ERROR_PREFIX: Final[str] = "Pi subprocess exited with code "
_PI_CHILD_WAVE_TIMEOUT_MS_ENV: Final[str] = "MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS"
_PI_TASK_PING_INTERVAL_MS_ENV: Final[str] = "MERIDIAN_PI_TASK_PING_INTERVAL_MS"
_PI_TASK_PING_RESET_ON_ACTIVITY_ENV: Final[str] = "MERIDIAN_PI_TASK_PING_RESET_ON_ACTIVITY"
_STREAM_LINE_KIND: Final[Literal["line"]] = "line"
_STREAM_EOF_KIND: Final[Literal["eof"]] = "eof"
_STREAM_ERROR_KIND: Final[Literal["error"]] = "error"


def pi_subprocess_exit_error(return_code: int) -> str:
    """Return the canonical failure emitted for a non-zero Pi process exit."""
    return f"{PI_SUBPROCESS_EXIT_ERROR_PREFIX}{return_code}."


def is_pi_subprocess_exit_error(error: str | None) -> bool:
    """Whether an error originated from the canonical Pi process-exit failure."""
    return error is not None and error.startswith(PI_SUBPROCESS_EXIT_ERROR_PREFIX)


class PiRpcConnection(HarnessConnection[ResolvedLaunchSpec]):
    """Full-duplex Pi RPC connection over JSONL stdio."""

    _CAPABILITIES = ConnectionCapabilities(
        mid_turn_injection="queue",
        supports_steer=True,
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
        self._stderr_log_path: Path | None = None
        self._event_stream_started = False
        self._session_id: str | None = None
        self._tracer = None
        self._startup_emitter: StartupPhaseEmitter | None = None
        self._abort_sent = False
        self._initial_prompt: str = ""
        self._initial_prompt_sent = False
        self._waiting_for_first_pi_event_after_prompt = False
        self._first_pi_event_received = False
        self._session_event_seen = False
        self._harness_ready_emitted = False
        self._pending_lifecycle_phase_events: list[HarnessEvent] = []
        self._launch_command: tuple[str, ...] = ()
        self._launch_cwd: str | None = None
        self._launch_session_role: str | None = None
        self._events_stream_active = False

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
        self._initial_prompt = config.prompt
        self._initial_prompt_sent = not bool(config.prompt.strip())
        self._waiting_for_first_pi_event_after_prompt = False
        self._first_pi_event_received = False
        self._session_event_seen = False
        self._harness_ready_emitted = False
        self._pending_lifecycle_phase_events = []
        self._launch_session_role = config.pi_session_role
        self._events_stream_active = False
        self._validate_initial_prompt_requirement()
        self._set_state("starting")

        try:
            await self._start_subprocess(config, spec)
            self._queue_lifecycle_phase_event(
                "process_spawned",
                pid=self.subprocess_pid,
                session_role=self._launch_session_role,
                cwd=self._launch_cwd,
                command=redact_pi_command_for_history(self._launch_command),
            )
            self._emit_startup_phase(StartupPhase.WAITING_FOR_CONNECTION)
            self._set_state("connected")
            sent_initial_prompt = await self._send_initial_prompt_if_pending()
            prompt_chars = len(self._initial_prompt)
            prompt_bytes = len(self._initial_prompt.encode("utf-8"))
            if sent_initial_prompt:
                self._queue_lifecycle_phase_event(
                    "initial_prompt_sent",
                    prompt_chars=prompt_chars,
                    prompt_bytes=prompt_bytes,
                )
                self._waiting_for_first_pi_event_after_prompt = True
                self._queue_lifecycle_phase_event(
                    "waiting_for_first_pi_event_after_prompt",
                    timeout_secs=_FIRST_STDOUT_AFTER_INITIAL_PROMPT_TIMEOUT_SECONDS,
                )
            else:
                self._queue_lifecycle_phase_event(
                    "no_initial_prompt",
                    prompt_chars=prompt_chars,
                    prompt_bytes=prompt_bytes,
                )
        except Exception:
            self._mark_failed("Pi RPC connection startup failed.")
            await self._cleanup_resources(terminate_process=True)
            raise

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        quiescent_stop = reason == "quiescent"
        if self._state == "stopped":
            return StopResult(escalated=False)
        if self._state not in {"stopping", "failed"}:
            self._set_state("stopping")

        await self._send_abort_message()
        exited_during_abort_grace = await self._wait_for_process_exit(
            timeout=_PROCESS_ABORT_GRACE_SECONDS
        )
        stop_escalated = False
        if exited_during_abort_grace:
            await self._cleanup_resources(terminate_process=True)
        else:
            if progress is not None:
                await progress(
                    "quiescent_stop_escalating",
                    {"reason": "abort_grace_expired"},
                )
            stop_escalated = await self._cleanup_resources(terminate_process=True)
        self._set_state("stopped")
        if quiescent_stop and stop_escalated:
            logger.info("Pi RPC quiescent stop escalated to process termination for cleanup")
        return StopResult(escalated=stop_escalated)

    def health(self) -> bool:
        return self._state == "connected"

    async def send_user_message(self, text: str) -> None:
        payload: dict[str, object] = {
            "type": "prompt",
            "message": text,
        }
        await self._send_rpc_message(payload, event="prompt")

    async def send_steer(self, text: str) -> None:
        payload: dict[str, object] = {
            "type": "steer",
            "message": text,
        }
        await self._send_rpc_message(payload, event="steer")

    async def send_cancel(self) -> None:
        if self._state in {"stopping", "stopped", "failed"}:
            return
        self._set_state("stopping")
        await self._send_abort_message()

    async def events(self) -> AsyncIterator[HarnessEvent]:
        process = self._process
        if process is None or process.stdout is None:
            return
        if self._event_stream_started:
            raise RuntimeError("events() iterator already consumed")
        self._event_stream_started = True
        self._events_stream_active = True
        stream_queue: asyncio.Queue[
            tuple[
                Literal["line", "eof", "error"],
                bytes | BaseException | None,
            ]
        ] = asyncio.Queue()
        stream_tasks = [
            asyncio.create_task(
                self._read_stdio_stream(
                    process.stdout,
                    queue=stream_queue,
                )
            )
        ]
        first_stdout_deadline_monotonic: float | None = None

        try:
            while self._pending_lifecycle_phase_events:
                yield self._pending_lifecycle_phase_events.pop(0)
            while True:
                now_monotonic = time.monotonic()
                try:
                    timeout_secs: float | None = None
                    if (
                        self._waiting_for_first_pi_event_after_prompt
                        and not self._first_pi_event_received
                    ):
                        if first_stdout_deadline_monotonic is None:
                            first_stdout_deadline_monotonic = (
                                time.monotonic()
                                + _FIRST_STDOUT_AFTER_INITIAL_PROMPT_TIMEOUT_SECONDS
                            )
                        timeout_secs = first_stdout_deadline_monotonic - now_monotonic
                        if timeout_secs <= 0:
                            raise TimeoutError
                    else:
                        first_stdout_deadline_monotonic = None
                    if timeout_secs is None:
                        stream_kind, payload = await stream_queue.get()
                    elif timeout_secs <= 0:
                        raise TimeoutError
                    else:
                        stream_kind, payload = await asyncio.wait_for(
                            stream_queue.get(),
                            timeout=timeout_secs,
                        )
                except TimeoutError:
                    now_monotonic = time.monotonic()
                    if (
                        not self._waiting_for_first_pi_event_after_prompt
                        or self._first_pi_event_received
                    ):
                        continue
                    if (
                        first_stdout_deadline_monotonic is None
                        or now_monotonic < first_stdout_deadline_monotonic
                    ):
                        continue
                    detail = self._failure_detail_with_stderr(_PI_FIRST_EVENT_TIMEOUT_REASON)
                    self._mark_failed(detail)
                    self._waiting_for_first_pi_event_after_prompt = False
                    yield self._lifecycle_phase_event(
                        "first_pi_event_timeout",
                        timeout_secs=_FIRST_STDOUT_AFTER_INITIAL_PROMPT_TIMEOUT_SECONDS,
                    )
                    yield self._error_event(detail)
                    await self._terminate_process()
                    break
                except Exception as exc:
                    if self._state not in {"stopping", "stopped"}:
                        detail = f"Failed to read Pi stdio stream: {exc}"
                        self._mark_failed(detail)
                        yield self._error_event(detail)
                    break

                if stream_kind == _STREAM_ERROR_KIND:
                    stream_error = payload
                    if self._state not in {"stopping", "stopped"}:
                        detail = f"Failed to read Pi stdout: {stream_error}"
                        self._mark_failed(detail)
                        yield self._error_event(detail)
                    break

                if stream_kind == _STREAM_EOF_KIND:
                    return_code = process.returncode
                    if return_code is None:
                        return_code = await process.wait()
                    if (
                        self._waiting_for_first_pi_event_after_prompt
                        and not self._first_pi_event_received
                        and self._state not in {"stopping", "stopped"}
                    ):
                        self._waiting_for_first_pi_event_after_prompt = False
                        detail = self._failure_detail_with_stderr(_PI_FIRST_EVENT_TIMEOUT_REASON)
                        self._mark_failed(detail)
                        yield self._lifecycle_phase_event(
                            "first_pi_event_eof_before_response",
                            return_code=return_code,
                        )
                        yield self._error_event(detail)
                        await self._terminate_process()
                        break
                    if return_code != 0 and self._state not in {"stopping", "stopped"}:
                        detail = self._failure_detail_with_stderr(
                            pi_subprocess_exit_error(return_code)
                        )
                        self._mark_failed(detail)
                        yield self._error_event(detail)
                    break

                if not isinstance(payload, bytes):
                    continue
                line_bytes = payload
                raw_text = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
                if not raw_text.strip():
                    continue

                trace_wire_recv(self._tracer, "stdout_line", raw_text, bytes=len(line_bytes))
                event = self._parse_stdout_line(raw_text)
                if event is None:
                    continue
                self._emit_harness_ready_once()
                if (
                    self._waiting_for_first_pi_event_after_prompt
                    and not self._first_pi_event_received
                ):
                    self._first_pi_event_received = True
                    self._waiting_for_first_pi_event_after_prompt = False
                    yield self._lifecycle_phase_event(
                        "first_pi_event_received",
                        first_event_type=event.event_type,
                    )
                if event.event_type == "session":
                    session_id = event.payload.get("id")
                    if isinstance(session_id, str) and session_id.strip():
                        self._session_id = session_id.strip()
                    if not self._session_event_seen:
                        self._session_event_seen = True
                        yield self._lifecycle_phase_event(
                            "session_event_seen",
                            session_id=self._session_id,
                        )
                yield event
            if not self._session_event_seen:
                yield self._lifecycle_phase_event("session_event_absent")
        finally:
            for task in stream_tasks:
                task.cancel()
            await asyncio.gather(*stream_tasks, return_exceptions=True)
            self._events_stream_active = False
            await self._cleanup_resources(terminate_process=False)

    async def _start_subprocess(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        spawn_dir = resolve_spawn_log_dir(config.control_root, config.spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)

        stderr_path = spawn_dir / "stderr.log"
        self._stderr_log_path = stderr_path
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
        env.update(pi_agent_dir_env_override())
        session_dir = env.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
        if session_dir:
            command = self._apply_session_dir_arg(command, session_dir)
        launch_role = "spawned"
        if (self._launch_session_role or "").strip().lower() == "primary":
            launch_role = "primary"
        self._apply_pi_child_wave_timeout_env(
            env=env,
            launch_role=launch_role,
            timeout_seconds=config.pi_child_wave_timeout_seconds,
        )
        self._apply_pi_task_ping_env(
            env=env,
            interval_seconds=config.pi_task_ping_interval_seconds,
            reset_on_activity=config.pi_task_ping_reset_on_activity,
        )
        try:
            resolved_runtime = resolve_pi_runtime(env=env, role=launch_role)
        except PiRuntimeResolutionError as exc:
            raise ConnectionNotReady(str(exc)) from exc
        command[0] = resolved_runtime.binary_path
        self._launch_command = tuple(command)
        self._launch_cwd = str(config.control_root)

        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._launch_cwd,
                env=env,
                stdin=PIPE,
                stdout=PIPE,
                stderr=self._stderr_handle,
                limit=_STDOUT_READLINE_LIMIT,
                start_new_session=not IS_WINDOWS,
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HarnessBinaryNotFound.from_os_error(
                harness_id=self.harness_id,
                error=exc,
                binary_name=command[0],
            ) from exc

    async def _read_stdio_stream(
        self,
        stream: asyncio.StreamReader,
        queue: asyncio.Queue[
            tuple[
                Literal["line", "eof", "error"],
                bytes | BaseException | None,
            ]
        ],
    ) -> None:
        try:
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    await queue.put((_STREAM_EOF_KIND, None))
                    return
                await queue.put((_STREAM_LINE_KIND, line_bytes))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put((_STREAM_ERROR_KIND, exc))

    def _apply_session_dir_arg(self, command: Sequence[str], session_dir: str) -> list[str]:
        rewritten: list[str] = []
        replaced = False
        i = 0
        while i < len(command):
            token = command[i]
            if token == _PI_SESSION_DIR_FLAG:
                rewritten.extend((_PI_SESSION_DIR_FLAG, session_dir))
                replaced = True
                i += 2
                continue
            if token.startswith(f"{_PI_SESSION_DIR_FLAG}="):
                rewritten.extend((_PI_SESSION_DIR_FLAG, session_dir))
                replaced = True
                i += 1
                continue
            rewritten.append(token)
            i += 1
        if not replaced:
            rewritten.extend((_PI_SESSION_DIR_FLAG, session_dir))
        return rewritten

    def _apply_pi_child_wave_timeout_env(
        self,
        *,
        env: dict[str, str],
        launch_role: str,
        timeout_seconds: float | None,
    ) -> None:
        if launch_role != "spawned":
            return
        if timeout_seconds is None or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            return
        timeout_ms = max(1, int(timeout_seconds * 1000))
        env[_PI_CHILD_WAVE_TIMEOUT_MS_ENV] = str(timeout_ms)

    def _apply_pi_task_ping_env(
        self,
        *,
        env: dict[str, str],
        interval_seconds: float | None,
        reset_on_activity: bool | None,
    ) -> None:
        if (
            interval_seconds is not None
            and math.isfinite(interval_seconds)
            and interval_seconds > 0
        ):
            interval_ms = max(1, int(interval_seconds * 1000))
            env[_PI_TASK_PING_INTERVAL_MS_ENV] = str(interval_ms)
        if reset_on_activity is not None:
            env[_PI_TASK_PING_RESET_ON_ACTIVITY_ENV] = (
                "true" if reset_on_activity else "false"
            )

    def _parse_stdout_line(self, line: str) -> HarnessEvent | None:
        payload_text = line.strip()
        if not payload_text:
            return None
        try:
            payload_obj = json.loads(payload_text)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed Pi stdout line: %s", payload_text)
            trace_parse_error(self._tracer, "pi", payload_text, error="malformed_json")
            return self._lifecycle_parse_error_event(
                reason="malformed_json",
                raw_line=payload_text,
            )
        if not isinstance(payload_obj, dict):
            logger.warning("Skipping non-object Pi stdout line: %s", payload_text)
            trace_parse_error(self._tracer, "pi", payload_text, error="non_object")
            return self._lifecycle_parse_error_event(
                reason="non_object",
                raw_line=payload_text,
            )

        payload = cast("dict[str, object]", payload_obj)
        event_type = payload.get("type")
        if not isinstance(event_type, str) or not event_type.strip():
            logger.warning("Skipping Pi stdout line without string 'type': %s", payload_text)
            trace_parse_error(self._tracer, "pi", payload_text, error="missing_type")
            return self._lifecycle_parse_error_event(
                reason="missing_type",
                raw_line=payload_text,
            )
        normalized_type = event_type.strip()
        if normalized_type.startswith(PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES):
            logger.warning(
                "Ignoring Pi lifecycle event on stdout; "
                "disk-backed task state is authoritative: %s",
                normalized_type,
            )
            return None
        if self._has_unsupported_lifecycle_schema_version(
            event_type=normalized_type,
            payload=payload,
        ):
            trace_parse_error(
                self._tracer,
                "pi",
                payload_text,
                error="unsupported_schema_version",
            )
            return self._lifecycle_parse_error_event(
                reason="unsupported_schema_version",
                error="unsupported_schema_version",
                raw_type=normalized_type,
                raw_line=payload_text,
            )

        return HarnessEvent(
            event_type=normalized_type,
            payload=payload,
            harness_id=_HARNESS_NAME,
            raw_text=line,
        )

    def _has_unsupported_lifecycle_schema_version(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        return has_unsupported_pi_lifecycle_schema_version(
            event_type=event_type,
            payload=payload,
        )

    def _truncate_parse_error_raw_line(self, raw_line: str) -> str:
        if len(raw_line) <= _PARSE_ERROR_RAW_LINE_LIMIT:
            return raw_line
        truncated = raw_line[:_PARSE_ERROR_RAW_LINE_LIMIT]
        return f"{truncated}…<truncated>"

    def _lifecycle_parse_error_event(
        self,
        *,
        reason: str,
        raw_line: str,
        error: str | None = None,
        raw_type: str | None = None,
    ) -> HarnessEvent:
        payload: dict[str, object] = {
            "type": "meridian.lifecycle.parse_error",
            "schema_version": PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION,
            "reason": reason,
            "raw_line": self._truncate_parse_error_raw_line(raw_line),
        }
        if error is not None:
            payload["error"] = error
        if raw_type is not None:
            payload["raw_type"] = raw_type
        return HarnessEvent(
            event_type="meridian.lifecycle.parse_error",
            payload=payload,
            harness_id=_HARNESS_NAME,
            raw_text=raw_line,
        )

    async def _send_rpc_message(self, payload: dict[str, object], *, event: str) -> None:
        if self._state != "connected":
            raise ConnectionNotReady("Pi RPC connection is not ready for send operations.")

        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise ConnectionNotReady("Pi RPC subprocess is not available for writes.")

        wire_payload = json.dumps(payload, separators=(",", ":")) + "\n"
        trace_wire_send(self._tracer, event, wire_payload)
        process.stdin.write(wire_payload.encode("utf-8"))
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ConnectionNotReady("Pi RPC subprocess stdin closed") from exc

    async def _send_abort_message(self) -> None:
        if self._abort_sent:
            return

        process = self._process
        if process is None or process.returncode is not None:
            return
        if process.stdin is None:
            return

        wire_payload = json.dumps({"type": "abort"}, separators=(",", ":")) + "\n"
        trace_wire_send(self._tracer, "abort", wire_payload)
        process.stdin.write(wire_payload.encode("utf-8"))
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Pi RPC subprocess closed before abort write completed", exc_info=True)
        self._abort_sent = True

    async def _send_initial_prompt_if_pending(self) -> bool:
        if self._initial_prompt_sent:
            return False
        prompt = self._initial_prompt
        if not prompt.strip():
            self._initial_prompt_sent = True
            return False
        await self.send_user_message(prompt)
        self._initial_prompt_sent = True
        return True

    def _validate_initial_prompt_requirement(self) -> None:
        if self._launch_session_role != "spawned":
            return
        if self._initial_prompt.strip():
            return
        raise ValueError(_PI_SPAWNED_PROMPT_REQUIRED_REASON)

    async def _wait_for_process_exit(self, *, timeout: float) -> bool:
        process = self._process
        if process is None or process.returncode is not None:
            return True
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

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

    def _read_stderr_snippet(self) -> str | None:
        if self._stderr_handle is not None:
            with suppress(OSError):
                self._stderr_handle.flush()
        stderr_path = self._stderr_log_path
        if stderr_path is None or not stderr_path.is_file():
            return None
        try:
            raw = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not raw:
            return None
        raw = compact_pi_failure_output(raw)
        if len(raw) <= _STDERR_SNIPPET_LIMIT:
            return raw
        return f"{raw[:_STDERR_SNIPPET_LIMIT]}…<truncated>"

    def _failure_detail_with_stderr(self, detail: str) -> str:
        stderr_snippet = self._read_stderr_snippet()
        if not stderr_snippet:
            return detail
        return f"{detail}\n\nPi subprocess stderr:\n{stderr_snippet}"

    def _mark_failed(self, reason: str) -> None:
        if self._state not in {"failed", "stopped"}:
            try:
                self._set_state("failed")
            except RuntimeError:
                logger.exception("Failed to transition Pi RPC connection into failed state")
        logger.warning("Pi RPC connection failed: %s", reason)

    def _error_event(self, message: str) -> HarnessEvent:
        return HarnessEvent(
            event_type="error/connectionClosed",
            payload={"type": "error/connectionClosed", "message": message},
            harness_id=_HARNESS_NAME,
        )

    async def _cleanup_resources(self, *, terminate_process: bool) -> bool:
        stop_escalated = False
        if terminate_process:
            stop_escalated = await self._terminate_process()
        if not self._events_stream_active:
            self._close_log_handles()
        return stop_escalated

    async def _terminate_process(self) -> bool:
        process = self._process
        if process is None:
            return False
        stop_escalated = False
        if process.returncode is None:
            stop_escalated = True
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except Exception:
                    logger.debug("Failed to close Pi RPC subprocess stdin", exc_info=True)
            if IS_WINDOWS:
                process.terminate()
            else:
                process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=_PROCESS_KILL_GRACE_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()
        self._process = None
        return stop_escalated

    def _close_log_handles(self) -> None:
        if self._stderr_handle is not None:
            with suppress(OSError):
                self._stderr_handle.flush()
            self._stderr_handle.close()
            self._stderr_handle = None
    def _emit_startup_phase(self, phase: StartupPhase) -> None:
        emitter = self._startup_emitter
        if emitter is not None:
            emitter.emit(phase)

    def _emit_harness_ready_once(self) -> None:
        if self._harness_ready_emitted:
            return
        self._emit_startup_phase(StartupPhase.HARNESS_READY)
        self._harness_ready_emitted = True

    def _lifecycle_phase_event(
        self,
        phase: str,
        **data: object,
    ) -> HarnessEvent:
        payload: dict[str, object] = {
            "type": _PI_PHASE_EVENT_TYPE,
            "phase": phase,
            "schema_version": PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION,
        }
        payload.update(data)
        if self._spawn_id:
            payload["spawn_id"] = str(self._spawn_id)
        event = HarnessEvent(
            event_type=_PI_PHASE_EVENT_TYPE,
            payload=payload,
            harness_id=_HARNESS_NAME,
            raw_text=None,
        )
        if self._tracer is not None:
            self._tracer.emit(
                "connection",
                "pi_lifecycle_phase",
                data=payload,
            )
        return event

    def _queue_lifecycle_phase_event(self, phase: str, **data: object) -> None:
        self._pending_lifecycle_phase_events.append(self._lifecycle_phase_event(phase, **data))


PiConnection = PiRpcConnection


__all__ = ["PiConnection", "PiRpcConnection"]
