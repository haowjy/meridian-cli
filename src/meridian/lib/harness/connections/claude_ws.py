"""Claude bidirectional connection adapter via stdin/stdout stream-json."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from asyncio.subprocess import PIPE, Process
from collections.abc import AsyncIterator
from io import BufferedWriter
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from meridian.lib.observability.debug_tracer import DebugTracer

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
    reap_on_ownership_transfer_failure,
    validate_prompt_size,
)
from meridian.lib.harness.connections.managed_backend import register_spawn_owned_process
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.harness.semantics import clears_signal
from meridian.lib.launch.constants import BASE_COMMAND_CLAUDE_STREAMING
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.observability.trace_helpers import (
    trace_parse_error,
    trace_state_change,
    trace_wire_recv,
    trace_wire_send,
)
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope import ProcessScopeSnapshot, ScopedProcessHandle
from meridian.lib.state.paths import resolve_project_runtime_root_for_write, resolve_spawn_log_dir

logger = logging.getLogger(__name__)

_PROCESS_KILL_GRACE_SECONDS: Final[float] = 10.0
_STDOUT_READLINE_LIMIT: Final[int] = 128 * 1024 * 1024  # 128 MiB
_VERSION_CHECK_TIMEOUT_SECONDS: Final[float] = 5.0
_STDERR_MAX_BYTES: Final[int] = 16 * 1024
_TESTED_VERSION_PREFIXES: Final[tuple[str, ...]] = ("1.", "2.")
_HARNESS_NAME: Final[str] = HarnessId.CLAUDE.value


class ClaudeConnection(HarnessConnection[ResolvedLaunchSpec]):
    """Bidirectional Claude harness connection via stdin/stdout stream-json.

    Launches ``claude -p --input-format stream-json --output-format stream-json``
    and communicates bidirectionally through the subprocess pipes:
    - Outbound user messages are written to stdin as NDJSON.
    - Inbound harness events are read from stdout as NDJSON.

    This replaces the earlier ``--sdk-url`` WebSocket approach, which used a
    flag that does not exist in current Claude CLI releases.
    """

    _CAPABILITIES = ConnectionCapabilities(
        mid_turn_injection="queue",
        supports_steer=False,
        supports_cancel=True,
        runtime_model_switch=False,
        structured_reasoning=True,
        supported_startup_phases=frozenset(
            phase.value
            for phase in (
                StartupPhase.LAUNCHING_SUBPROCESS,
                StartupPhase.WAITING_FOR_CONNECTION,
                StartupPhase.SENDING_PROMPT,
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
        self._config: ConnectionConfig | None = None
        self._process: Process | None = None
        self._scope_handle: ScopedProcessHandle | None = None
        self._send_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._stderr_handle: BufferedWriter | None = None
        self._stderr_log_path: Path | None = None
        self._protocol_validated = False
        self._event_stream_started = False
        self._tracer: DebugTracer | None = None
        self._cancel_requested = False
        self._signal_in_flight = False
        self._startup_emitter: StartupPhaseEmitter | None = None

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CLAUDE

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

    @property
    def scope_snapshot(self) -> ProcessScopeSnapshot | None:
        handle = self._scope_handle
        return None if handle is None else handle.snapshot

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        """Launch Claude subprocess and send the initial user prompt via stdin."""

        if self._state != "created":
            raise RuntimeError(f"Connection can only start from 'created', got '{self._state}'")

        validate_prompt_size(config)

        self._config = config
        self._spawn_id = config.spawn_id
        self._tracer = config.debug_tracer
        self._startup_emitter = StartupPhaseEmitter(
            str(config.spawn_id),
            harness_id=config.harness_id.value,
            model=spec.model,
            agent=spec.agent_name,
        )
        self._cancel_requested = False
        self._signal_in_flight = False
        self._set_state("starting")

        try:
            await self._check_claude_version(config.child_env)
            self._emit_startup_phase(StartupPhase.LAUNCHING_SUBPROCESS)
            await self._start_subprocess(config, spec)
            self._emit_startup_phase(StartupPhase.WAITING_FOR_CONNECTION)
            self._emit_startup_phase(StartupPhase.SENDING_PROMPT)
            await self._send_user_turn(config.prompt)
            self._set_state("connected")
        except BaseException:
            self._mark_failed("Claude connection startup failed.")
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
        """Stop the subprocess. Safe to call multiple times."""
        _ = reason, progress

        async with self._stop_lock:
            if self._state == "stopped":
                return StopResult()

            if self._state not in {"stopping", "failed"}:
                self._set_state("stopping")

            await self._cleanup_resources(terminate_process=True)
            self._cancel_requested = False
            self._signal_in_flight = False
            self._set_state("stopped")
            return StopResult()

    def health(self) -> bool:
        return self._state == "connected"

    async def send_user_message(self, text: str) -> None:
        self._ensure_connected()
        self._signal_in_flight = False
        await self._send_user_turn(text)

    async def send_cancel(self) -> None:
        """Signal cancellation by transitioning state and sending SIGINT."""
        if self._cancel_requested:
            return
        if self._state in {"stopping", "stopped", "failed"}:
            self._cancel_requested = True
            return
        self._ensure_connected()
        self._cancel_requested = True
        self._signal_in_flight = True
        if self._state != "stopping":
            self._set_state("stopping")
        await self._signal_process(signal.SIGINT)

    async def events(self) -> AsyncIterator[HarnessEvent]:
        """Yield HarnessEvent objects read line-by-line from Claude stdout."""

        process = self._process
        if process is None:
            return
        stdout = process.stdout
        if stdout is None:
            return
        if self._event_stream_started:
            raise RuntimeError("events() iterator already consumed")
        self._event_stream_started = True

        try:
            while True:
                try:
                    line_bytes = await stdout.readline()
                except Exception as exc:
                    if self._state not in {"stopping", "stopped"}:
                        detail = f"Failed to read Claude stdout: {exc}"
                        self._mark_failed(detail)
                        yield self._error_event(detail)
                    return

                if not line_bytes:
                    # EOF — subprocess has exited and all output has been drained.
                    return_code = process.returncode
                    if return_code is None:
                        return_code = await process.wait()
                    if return_code != 0 and self._state not in {"stopping", "stopped"}:
                        detail = f"Claude subprocess exited with code {return_code}."
                        stderr_excerpt = self._read_stderr_excerpt()
                        if stderr_excerpt:
                            detail = f"{detail}\n\nClaude subprocess stderr:\n{stderr_excerpt}"
                        self._mark_failed(detail)
                        yield self._error_event(detail)
                    return

                raw_text = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
                if not raw_text.strip():
                    continue

                trace_wire_recv(self._tracer, "stdout_line", raw_text, bytes=len(line_bytes))
                parsed_events = self._parse_stdout_line(raw_text)
                if not self._protocol_validated:
                    if not parsed_events:
                        detail = (
                            "Protocol mismatch: first Claude stdout line did not contain "
                            "valid typed JSON."
                        )
                        self._mark_failed(detail)
                        yield self._error_event(detail, raw_text=raw_text)
                        return
                    self._protocol_validated = True
                    self._emit_startup_phase(StartupPhase.HARNESS_READY)

                for event in parsed_events:
                    if clears_signal(event):
                        self._signal_in_flight = False
                    if self._tracer is not None:
                        self._tracer.emit(
                            "wire",
                            "parsed_event",
                            direction="inbound",
                            data={"event_type": event.event_type},
                        )
                    yield event
        finally:
            pass

    def _ensure_connected(self) -> None:
        if self._state != "connected":
            raise ConnectionNotReady(
                f"Claude connection is not ready (state={self._state}); expected 'connected'."
            )

    def _set_state(self, next_state: ConnectionState) -> None:
        if next_state == self._state:
            return
        allowed = self._ALLOWED_TRANSITIONS[self._state]
        if next_state not in allowed:
            raise RuntimeError(
                f"Invalid connection state transition: {self._state} -> {next_state}"
            )
        trace_state_change(self._tracer, "claude", self._state, next_state)
        self._state = next_state

    def _mark_failed(self, reason: str) -> None:
        if self._state not in {"failed", "stopped"}:
            try:
                self._set_state("failed")
            except RuntimeError:
                logger.exception("Failed to transition Claude connection into failed state")
        logger.warning("Claude connection failed: %s", reason)

    async def _check_claude_version(self, child_env: dict[str, str]) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                "claude",
                "--version",
                env=dict(child_env),
                stdout=PIPE,
                stderr=PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=_VERSION_CHECK_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("Timed out while checking Claude CLI version.")
            return
        except OSError:
            logger.warning("Could not execute `claude --version`; skipping version gate.")
            return

        output = (stdout + stderr).decode("utf-8", errors="ignore").strip()
        version = self._extract_semver(output)
        if version is None:
            logger.warning("Unknown Claude version output: %s", output or "<empty>")
            return
        if not version.startswith(_TESTED_VERSION_PREFIXES):
            logger.warning(
                "Claude version may be untested for bidirectional stdin/stdout protocol: %s",
                version,
            )

    @staticmethod
    def _extract_semver(text: str) -> str | None:
        for token in text.split():
            parts = token.strip().split(".")
            if len(parts) < 2:
                continue
            if all(part.isdigit() for part in parts[:2]):
                return token.strip()
        return None

    async def _start_subprocess(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        spawn_dir = resolve_spawn_log_dir(
            config.control_root,
            config.spawn_id,
            runtime_root=(
                config.runtime_root
                or resolve_project_runtime_root_for_write(config.control_root)
            ),
        )

        stderr_path = spawn_dir / "stderr.log"
        self._stderr_log_path = stderr_path
        self._stderr_handle = stderr_path.open("ab")

        command = self._build_command(config, spec)

        env = dict(config.child_env)

        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(config.control_root),
                env=env,
                stdin=PIPE,
                stdout=PIPE,
                stderr=self._stderr_handle,
                limit=_STDOUT_READLINE_LIMIT,
                start_new_session=not IS_WINDOWS,
            )
            self._scope_handle = await register_spawn_owned_process(
                spawn_id=config.spawn_id,
                control_root=config.control_root,
                process=self._process,
                scope_id="stdio",
                role="harness_stdio",
                runtime_root=config.runtime_root,
                persist=config.runtime_root is not None,
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HarnessBinaryNotFound.from_os_error(
                harness_id=self.harness_id,
                error=exc,
                binary_name=command[0],
            ) from exc

    def _build_command(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> list[str]:
        _ = config
        return project_subprocess_spec(
            self.harness_id,
            spec,
            base_command=BASE_COMMAND_CLAUDE_STREAMING,
        )

    async def _send_user_turn(self, text: str) -> None:
        """Send a user turn in the stream-json wire format Claude expects.

        Claude's ``--input-format stream-json`` protocol wraps each user message as::

            {"type":"user","message":{"role":"user","content":"<text>"}}
        """
        await self._send_json({"type": "user", "message": {"role": "user", "content": text}})

    async def _send_json(self, payload: dict[str, object]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise ConnectionNotReady("Claude subprocess stdin is not available.")

        wire = json.dumps(payload, separators=(",", ":")) + "\n"
        trace_wire_send(self._tracer, "stdin_write", wire, bytes=len(wire.encode("utf-8")))
        async with self._send_lock:
            process.stdin.write(wire.encode("utf-8"))
            await process.stdin.drain()

    async def _signal_process(self, sig: signal.Signals) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        trace_wire_send(self._tracer, "signal_sent", "", signal=sig.name)
        if IS_WINDOWS:
            # GenerateConsoleCtrlEvent(CTRL_C_EVENT) is unreliable for
            # non-console-sharing processes; use TerminateProcess instead.
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
        scope_handle = self._scope_handle
        self._scope_handle = None
        if scope_handle is not None and process.returncode is None:
            await scope_handle.terminate(
                grace_seconds=_PROCESS_KILL_GRACE_SECONDS,
                reason="claude_connection_stop",
            )
            self._process = None
            return
        if process.returncode is None:
            # Close stdin to signal no more input; then terminate if needed.
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except Exception:
                    logger.debug("Failed to close Claude subprocess stdin", exc_info=True)
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
        self._stderr_log_path = None

    def _read_stderr_excerpt(self) -> str:
        if self._stderr_handle is not None:
            self._stderr_handle.flush()
        path = self._stderr_log_path
        if path is None or not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end_offset = handle.tell()
            handle.seek(max(0, end_offset - _STDERR_MAX_BYTES), os.SEEK_SET)
            data = handle.read()
        return data.decode("utf-8", errors="replace").strip()

    def _parse_stdout_line(self, line: str) -> list[HarnessEvent]:
        """Parse one line of NDJSON from Claude stdout into HarnessEvent objects."""
        payload_text = line.strip()
        if not payload_text:
            return []
        try:
            payload_obj = json.loads(payload_text)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed Claude stdout line: %s", payload_text)
            trace_parse_error(self._tracer, "claude", payload_text, error="malformed_json")
            return []
        if not isinstance(payload_obj, dict):
            logger.warning("Skipping non-object Claude stdout line: %s", payload_text)
            trace_parse_error(self._tracer, "claude", payload_text, error="non_object")
            return []

        payload = cast("dict[str, object]", payload_obj)
        event_type = payload.get("type")
        if not isinstance(event_type, str) or not event_type.strip():
            logger.warning(
                "Skipping Claude stdout line without string 'type': %s",
                payload_text,
            )
            trace_parse_error(self._tracer, "claude", payload_text, error="missing_type")
            return []

        return [
            HarnessEvent(
                event_type=event_type,
                payload=payload,
                harness_id=_HARNESS_NAME,
                raw_text=line,
            )
        ]

    def _error_event(self, message: str, raw_text: str | None = None) -> HarnessEvent:
        payload: dict[str, object] = {
            "type": "error/connectionClosed",
            "message": message,
        }
        return HarnessEvent(
            event_type="error/connectionClosed",
            payload=payload,
            harness_id=_HARNESS_NAME,
            raw_text=raw_text,
        )

    def _emit_startup_phase(self, phase: StartupPhase) -> None:
        emitter = self._startup_emitter
        if emitter is not None:
            emitter.emit(phase)


__all__ = ["ClaudeConnection"]
