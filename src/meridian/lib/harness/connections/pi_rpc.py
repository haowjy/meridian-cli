"""Pi RPC stdio connection implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from asyncio.subprocess import PIPE
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal, NamedTuple, cast

from meridian.lib.core.telemetry import StartupPhase, StartupPhaseEmitter
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.bundle import project_subprocess_spec
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionNotReady,
    ConnectionState,
    HarnessConnection,
    RawHarnessEvent,
    StopProgressCallback,
    StopResult,
    reap_on_ownership_transfer_failure,
    validate_prompt_size,
)
from meridian.lib.harness.connections.managed_stdio import (
    ManagedStdioProcess,
    launch_managed_stdio,
)
from meridian.lib.harness.pi_failure import compact_pi_failure_output
from meridian.lib.harness.pi_lifecycle_events import (
    PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES,
    PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION,
    has_unsupported_pi_lifecycle_schema_version,
    redact_pi_command_for_history,
)
from meridian.lib.harness.pi_runtime_resolver import (
    PiRuntimeResolutionError,
    resolve_pi_runtime,
)
from meridian.lib.launch.constants import BASE_COMMAND_PI_SUBPROCESS
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.observability.trace_helpers import (
    trace_parse_error,
    trace_state_change,
    trace_wire_recv,
    trace_wire_send,
)
from meridian.lib.platform.process_scope import ProcessScopeSnapshot

logger = logging.getLogger(__name__)
_HARNESS_NAME: Final = HarnessId.PI.value
_STDOUT_READLINE_LIMIT: Final[int] = 10 * 1024 * 1024
_PARSE_ERROR_RAW_LINE_LIMIT: Final[int] = 2048
_STDERR_SNIPPET_LIMIT: Final[int] = 4096
_PI_PHASE_EVENT_TYPE: Final[str] = "meridian.pi.lifecycle.phase"
_PI_FIRST_EVENT_TIMEOUT_REASON: Final[str] = "pi_rpc_no_response_after_initial_prompt"
_PI_STREAM_CLOSED_BEFORE_FIRST_EVENT_REASON: Final[str] = (
    "pi_rpc_stream_closed_before_initial_response"
)
_PI_SPAWNED_PROMPT_REQUIRED_REASON: Final[str] = "pi_rpc_spawned_prompt_required"
_PI_SESSION_DIR_FLAG: Final[str] = "--session-dir"
PI_SUBPROCESS_EXIT_ERROR_PREFIX: Final[str] = "Pi subprocess exited with code "
_STREAM_LINE_KIND: Final[Literal["line"]] = "line"
_STREAM_EOF_KIND: Final[Literal["eof"]] = "eof"
_STREAM_ERROR_KIND: Final[Literal["error"]] = "error"


def pi_subprocess_exit_error(return_code: int) -> str:
    """Return the canonical failure emitted for a non-zero Pi process exit."""
    return f"{PI_SUBPROCESS_EXIT_ERROR_PREFIX}{return_code}."


def is_pi_subprocess_exit_error(error: str | None) -> bool:
    """Whether an error originated from the canonical Pi process-exit failure."""
    return error is not None and error.startswith(PI_SUBPROCESS_EXIT_ERROR_PREFIX)


class ParsedStdoutLine(NamedTuple):
    """A parsed stdout event and whether it belongs to the Pi protocol."""

    event: RawHarnessEvent | None
    is_protocol_event: bool


@dataclass(frozen=True)
class PiRpcTimingPolicy:
    first_event_timeout_seconds: float = 30.0
    abort_grace_seconds: float = 5.0
    kill_grace_seconds: float = 5.0


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
                StartupPhase.LAUNCHING_SUBPROCESS,
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

    def __init__(
        self,
        *,
        timing: PiRpcTimingPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timing = timing or PiRpcTimingPolicy()
        self._clock = clock
        self._state: ConnectionState = "created"
        self._spawn_id: SpawnId = SpawnId("")
        self._child: ManagedStdioProcess | None = None
        self._event_stream_started = False
        self._session_id: str | None = None
        self._tracer = None
        self._startup_emitter: StartupPhaseEmitter | None = None
        self._abort_sent = False
        self._initial_prompt: str = ""
        self._initial_prompt_sent = False
        self._waiting_for_first_pi_event_after_prompt = False
        self._first_event_deadline_monotonic: float | None = None
        self._first_pi_event_received = False
        self._session_event_seen = False
        self._harness_ready_emitted = False
        self._pending_lifecycle_phase_events: list[RawHarnessEvent] = []
        self._launch_command: tuple[str, ...] = ()
        self._launch_cwd: str | None = None
        self._launch_session_role: str | None = None
        self._events_stream_active = False
        self._prompt_command_seq = 0
        self._pending_prompt_acks: dict[str, asyncio.Future[None]] = {}
        self._stop_lock = asyncio.Lock()

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
        return None if self._child is None else self._child.pid

    @property
    def scope_snapshot(self) -> ProcessScopeSnapshot | None:
        return None if self._child is None else self._child.scope_snapshot

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
        self._first_event_deadline_monotonic = None
        self._first_pi_event_received = False
        self._session_event_seen = False
        self._harness_ready_emitted = False
        self._pending_lifecycle_phase_events = []
        self._launch_session_role = config.pi_session_role
        self._events_stream_active = False
        self._validate_initial_prompt_requirement()
        self._set_state("starting")

        try:
            self._emit_startup_phase(StartupPhase.LAUNCHING_SUBPROCESS)
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
                self._first_event_deadline_monotonic = (
                    self._clock() + self._timing.first_event_timeout_seconds
                )
                self._queue_lifecycle_phase_event(
                    "waiting_for_first_pi_event_after_prompt",
                    timeout_secs=self._timing.first_event_timeout_seconds,
                )
            else:
                self._queue_lifecycle_phase_event(
                    "no_initial_prompt",
                    prompt_chars=prompt_chars,
                    prompt_bytes=prompt_bytes,
                )
        except BaseException:
            self._mark_failed("Pi RPC connection startup failed.")
            await reap_on_ownership_transfer_failure(self._cleanup_start_failure)
            raise

    async def _cleanup_start_failure(self) -> None:
        async with self._stop_lock:
            await self._cleanup_resources(terminate_process=True)

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        async with self._stop_lock:
            return await self._stop_unlocked(reason=reason, progress=progress)

    async def _stop_unlocked(
        self,
        *,
        reason: str | None,
        progress: StopProgressCallback | None,
    ) -> StopResult:
        quiescent_stop = reason == "quiescent"
        if self._state == "stopped":
            return StopResult(escalated=False)
        if self._state not in {"stopping", "failed"}:
            self._set_state("stopping")

        await self._send_abort_message()
        child = self._child
        exited_during_abort_grace = child is None or await child.wait_for_exit(
            timeout=self._timing.abort_grace_seconds
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
        await self._send_prompt(text, wait_for_ack=True)

    async def _send_prompt(self, text: str, *, wait_for_ack: bool) -> None:
        command_id = f"meridian-prompt-{self._prompt_command_seq}"
        self._prompt_command_seq += 1
        payload: dict[str, object] = {
            "id": command_id,
            "type": "prompt",
            "message": text,
            "streamingBehavior": "followUp",
        }
        if not wait_for_ack:
            await self._send_rpc_message(payload, event="prompt")
            return

        ack = asyncio.get_running_loop().create_future()
        self._pending_prompt_acks[command_id] = ack
        try:
            await self._send_rpc_message(payload, event="prompt")
            await ack
        finally:
            self._pending_prompt_acks.pop(command_id, None)

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

    async def events(self) -> AsyncIterator[RawHarnessEvent]:
        child = self._child
        if child is None:
            return
        process = child.process
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
        try:
            while self._pending_lifecycle_phase_events:
                yield self._pending_lifecycle_phase_events.pop(0)
            while True:
                now_monotonic = self._clock()
                try:
                    timeout_secs: float | None = None
                    if (
                        self._waiting_for_first_pi_event_after_prompt
                        and not self._first_pi_event_received
                    ):
                        deadline = self._first_event_deadline_monotonic
                        if deadline is None:
                            raise RuntimeError("Missing Pi first-event deadline")
                        timeout_secs = deadline - now_monotonic
                        if timeout_secs <= 0:
                            raise TimeoutError
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
                    now_monotonic = self._clock()
                    if (
                        not self._waiting_for_first_pi_event_after_prompt
                        or self._first_pi_event_received
                    ):
                        continue
                    if (
                        self._first_event_deadline_monotonic is None
                        or now_monotonic < self._first_event_deadline_monotonic
                    ):
                        continue
                    detail = self._failure_detail_with_stderr(_PI_FIRST_EVENT_TIMEOUT_REASON)
                    self._mark_failed(detail)
                    self._waiting_for_first_pi_event_after_prompt = False
                    self._first_event_deadline_monotonic = None
                    yield self._lifecycle_phase_event(
                        "first_pi_event_timeout",
                        timeout_secs=self._timing.first_event_timeout_seconds,
                    )
                    yield self._error_event(detail)
                    await child.terminate()
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
                        self._first_event_deadline_monotonic = None
                        reason = (
                            pi_subprocess_exit_error(return_code)
                            if return_code not in (0, None)
                            else _PI_STREAM_CLOSED_BEFORE_FIRST_EVENT_REASON
                        )
                        detail = self._failure_detail_with_stderr(reason)
                        self._mark_failed(detail)
                        yield self._lifecycle_phase_event(
                            "first_pi_event_eof_before_response",
                            return_code=return_code,
                        )
                        yield self._error_event(detail)
                        await child.terminate()
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
                parsed = self._parse_stdout_line(raw_text)
                event = parsed.event
                if event is None:
                    continue
                if parsed.is_protocol_event:
                    self._emit_harness_ready_once()
                    if (
                        self._waiting_for_first_pi_event_after_prompt
                        and not self._first_pi_event_received
                    ):
                        self._first_pi_event_received = True
                        self._waiting_for_first_pi_event_after_prompt = False
                        self._first_event_deadline_monotonic = None
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
        command = project_subprocess_spec(
            self.harness_id,
            spec,
            base_command=BASE_COMMAND_PI_SUBPROCESS,
        )
        env = dict(config.child_env)
        session_dir = env.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
        if session_dir:
            command = self._apply_session_dir_arg(command, session_dir)
        launch_role = "spawned"
        if (self._launch_session_role or "").strip().lower() == "primary":
            launch_role = "primary"
        try:
            resolved_runtime = resolve_pi_runtime(env=env, role=launch_role)
        except PiRuntimeResolutionError as exc:
            raise ConnectionNotReady(str(exc)) from exc
        command[0] = resolved_runtime.binary_path
        self._launch_command = tuple(command)
        self._launch_cwd = str(config.control_root)
        self._child = await launch_managed_stdio(
            config=config,
            harness_id=self.harness_id,
            command=command,
            env=env,
            cwd=self._launch_cwd,
            stdin=PIPE,
            stdout_limit=_STDOUT_READLINE_LIMIT,
            kill_grace_seconds=self._timing.kill_grace_seconds,
            terminate_reason="pi_connection_stop",
        )

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

    def _parse_stdout_line(self, line: str) -> ParsedStdoutLine:
        payload_text = line.strip()
        if not payload_text:
            return ParsedStdoutLine(None, False)
        try:
            payload_obj = json.loads(payload_text)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed Pi stdout line: %s", payload_text)
            trace_parse_error(self._tracer, "pi", payload_text, error="malformed_json")
            return ParsedStdoutLine(
                self._lifecycle_parse_error_event(
                    reason="malformed_json",
                    raw_line=payload_text,
                ),
                False,
            )
        if not isinstance(payload_obj, dict):
            logger.warning("Skipping non-object Pi stdout line: %s", payload_text)
            trace_parse_error(self._tracer, "pi", payload_text, error="non_object")
            return ParsedStdoutLine(
                self._lifecycle_parse_error_event(
                    reason="non_object",
                    raw_line=payload_text,
                ),
                False,
            )

        payload = cast("dict[str, object]", payload_obj)
        event_type = payload.get("type")
        if not isinstance(event_type, str) or not event_type.strip():
            logger.warning("Skipping Pi stdout line without string 'type': %s", payload_text)
            trace_parse_error(self._tracer, "pi", payload_text, error="missing_type")
            return ParsedStdoutLine(
                self._lifecycle_parse_error_event(
                    reason="missing_type",
                    raw_line=payload_text,
                ),
                False,
            )
        normalized_type = event_type.strip()
        if normalized_type.startswith(PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES):
            logger.warning(
                "Ignoring Pi lifecycle event on stdout; "
                "disk-backed task state is authoritative: %s",
                normalized_type,
            )
            return ParsedStdoutLine(None, False)
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
            return ParsedStdoutLine(
                self._lifecycle_parse_error_event(
                    reason="unsupported_schema_version",
                    error="unsupported_schema_version",
                    raw_type=normalized_type,
                    raw_line=payload_text,
                ),
                False,
            )

        if normalized_type == "response" and self._resolve_prompt_ack(payload):
            payload = dict(payload)
            payload["meridian_control_action"] = "inject"

        return ParsedStdoutLine(
            RawHarnessEvent(
                event_type=normalized_type,
                payload=payload,
                harness_id=_HARNESS_NAME,
                raw_text=line,
            ),
            True,
        )

    def _resolve_prompt_ack(self, payload: dict[str, object]) -> bool:
        if str(payload.get("command", "")).strip().lower() != "prompt":
            return False
        command_id = payload.get("id")
        if not isinstance(command_id, str):
            return False
        pending = self._pending_prompt_acks.get(command_id)
        if pending is None or pending.done():
            return False
        if payload.get("success") is True:
            pending.set_result(None)
        else:
            error = payload.get("error")
            detail = (
                error.strip()
                if isinstance(error, str) and error.strip()
                else "pi_prompt_rejected"
            )
            pending.set_exception(RuntimeError(detail))
        return True

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
    ) -> RawHarnessEvent:
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
        return RawHarnessEvent(
            event_type="meridian.lifecycle.parse_error",
            payload=payload,
            harness_id=_HARNESS_NAME,
            raw_text=raw_line,
        )

    async def _send_rpc_message(self, payload: dict[str, object], *, event: str) -> None:
        if self._state != "connected":
            raise ConnectionNotReady("Pi RPC connection is not ready for send operations.")

        child = self._child
        process = None if child is None else child.process
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

        child = self._child
        process = None if child is None else child.process
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
        await self._send_prompt(prompt, wait_for_ack=False)
        self._initial_prompt_sent = True
        return True

    def _validate_initial_prompt_requirement(self) -> None:
        if self._launch_session_role != "spawned":
            return
        if self._initial_prompt.strip():
            return
        raise ValueError(_PI_SPAWNED_PROMPT_REQUIRED_REASON)

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
        child = self._child
        if child is None:
            return None
        tail = child.read_stderr_tail()
        if not tail:
            return None
        compacted = compact_pi_failure_output(tail)
        if len(compacted) <= _STDERR_SNIPPET_LIMIT:
            return compacted
        return f"{compacted[:_STDERR_SNIPPET_LIMIT]}…<truncated>"

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

    def _error_event(self, message: str) -> RawHarnessEvent:
        return RawHarnessEvent(
            event_type="meridian/error/connectionClosed",
            payload={"type": "meridian/error/connectionClosed", "message": message},
            harness_id=_HARNESS_NAME,
        )

    async def _cleanup_resources(self, *, terminate_process: bool) -> bool:
        self._fail_pending_prompt_acks()
        stop_escalated = False
        if terminate_process:
            child = self._child
            if child is not None:
                stop_escalated = await child.terminate()
        if not self._events_stream_active and self._child is not None:
            self._child.close_stderr_handle()
        return stop_escalated

    def _fail_pending_prompt_acks(self) -> None:
        for pending in self._pending_prompt_acks.values():
            if not pending.done():
                pending.set_exception(
                    ConnectionNotReady(
                        "Pi RPC connection closed before prompt acknowledgement."
                    )
                )

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
    ) -> RawHarnessEvent:
        payload: dict[str, object] = {
            "type": _PI_PHASE_EVENT_TYPE,
            "phase": phase,
            "schema_version": PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION,
        }
        payload.update(data)
        if self._spawn_id:
            payload["spawn_id"] = str(self._spawn_id)
        event = RawHarnessEvent(
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


__all__ = ["PiConnection", "PiRpcConnection", "PiRpcTimingPolicy"]
