"""Bidirectional Codex websocket connection adapter."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import json
import logging
import os
import socket
import time as _time
from asyncio.subprocess import Process
from collections.abc import AsyncIterator, Awaitable, Callable
from io import BufferedWriter
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

from aiohttp import ClientSession, WSMsgType

if TYPE_CHECKING:
    from meridian.lib.observability.debug_tracer import DebugTracer

from meridian import __version__
from meridian.lib.core.telemetry import StartupPhase, StartupPhaseEmitter
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.bundle import (
    project_managed_primary_backend_command,
    project_managed_primary_bootstrap,
)
from meridian.lib.harness.codex_rollout import (
    find_attachable_rollout_session_id,
    resolve_codex_home,
)
from meridian.lib.harness.connections.base import (
    MAX_HARNESS_MESSAGE_BYTES,
    AutoAcceptHandler,
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionNotReady,
    ConnectionState,
    HarnessConnection,
    HarnessRequest,
    InteractiveHandler,
    ObserverEndpoint,
    PrimaryRuntimeRequestPolicy,
    RawHarnessEvent,
    ServerRequestHandler,
    StopProgressCallback,
    StopResult,
    reap_on_ownership_transfer_failure,
    validate_prompt_size,
)
from meridian.lib.harness.connections.errors import PortBindError
from meridian.lib.harness.connections.liveness import (
    BackendLivenessPolicy,
    EventStreamLivenessTimeout,
)
from meridian.lib.harness.connections.managed_backend import (
    ManagedBackendConfig,
    launch_managed_backend,
)
from meridian.lib.harness.connections.resident_backend import (
    LivenessResidentBackendControl,
    ResidentBackendControl,
)
from meridian.lib.harness.semantics import (
    EventSemantics,
    PrimaryEventScope,
)
from meridian.lib.launch.env import inherit_child_env
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.observability.trace_helpers import (
    trace_parse_error,
    trace_state_change,
    trace_wire_send,
)
from meridian.lib.platform.detached_process import ParentDeathLink, release_parent_death_link
from meridian.lib.platform.process_scope import (
    ProcessScopeSnapshot,
    ScopedProcessHandle,
)
from meridian.lib.state.paths import resolve_spawn_log_dir

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
_STOP_WAIT_TIMEOUT_SECONDS = 5.0
_STARTUP_STDERR_MAX_BYTES = 16 * 1024
_ADDRESS_IN_USE_MARKERS: Final[tuple[str, ...]] = (
    "address already in use",
    "address in use",
    "eaddrinuse",
)
_BOOTSTRAP_TURN_PROMPT: Final[str] = (
    "Meridian started"
)


def _load_websockets_module() -> Any | None:
    if importlib.util.find_spec("websockets") is None:
        return None
    return importlib.import_module("websockets")


_WEBSOCKETS_MODULE: Any | None = _load_websockets_module()
logger = logging.getLogger(__name__)


def _ws_is_open(ws: object) -> bool:
    """Handle websockets state checks across API versions."""
    state = getattr(ws, "state", None)
    if state is not None:
        state_name = getattr(state, "name", None)
        if isinstance(state_name, str):
            return state_name == "OPEN"

        state_value = getattr(state, "value", None)
        if isinstance(state_value, int):
            return state_value == 1

        if isinstance(state, int):
            return state == 1

    closed = getattr(ws, "closed", None)
    if isinstance(closed, bool):
        return not closed

    return False


def _is_clean_websocket_close(exc: BaseException) -> bool:
    """Return whether a websocket close exception represents normal closure."""

    exceptions_module = getattr(_WEBSOCKETS_MODULE, "exceptions", None)
    connection_closed_ok = getattr(exceptions_module, "ConnectionClosedOK", None)
    if connection_closed_ok is not None and isinstance(exc, connection_closed_ok):
        return True

    connection_closed_error = getattr(exceptions_module, "ConnectionClosedError", None)
    if connection_closed_error is None or not isinstance(exc, connection_closed_error):
        return False

    for attr in ("rcvd", "sent"):
        close_frame = getattr(exc, attr, None)
        if getattr(close_frame, "code", None) == 1000:
            return True
    return False


class _AiohttpWebSocketCompat:
    """Compatibility layer matching the subset of the websockets API we use."""

    def __init__(self, session: ClientSession, ws: Any) -> None:
        self._session = session
        self._ws = ws

    @property
    def closed(self) -> bool:
        return bool(self._ws.closed)

    async def send(self, data: str) -> None:
        await self._ws.send_str(data)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close()
        await self._session.close()

    def __aiter__(self) -> _AiohttpWebSocketCompat:
        return self

    async def __anext__(self) -> str | bytes:
        while True:
            message = await self._ws.receive()
            msg_type = message.type
            if msg_type is WSMsgType.TEXT:
                return cast("str", message.data)
            if msg_type is WSMsgType.BINARY:
                return cast("bytes", message.data)
            if msg_type in {WSMsgType.CLOSE, WSMsgType.CLOSED}:
                raise StopAsyncIteration
            if msg_type is WSMsgType.ERROR:
                raise RuntimeError("Aiohttp websocket reported an error")


async def _aiohttp_connect(ws_url: str) -> _AiohttpWebSocketCompat:
    session = ClientSession()
    try:
        ws = await session.ws_connect(ws_url, max_msg_size=MAX_HARNESS_MESSAGE_BYTES)
    except Exception:
        await session.close()
        raise
    return _AiohttpWebSocketCompat(session, ws)


class CodexConnection(HarnessConnection[ResolvedLaunchSpec]):
    """JSON-RPC 2.0 bridge between Meridian and Codex app-server."""

    _LIVENESS_TIMEOUT_SECONDS: ClassVar[float] = 120.0

    _ALLOWED_TRANSITIONS: Final[dict[ConnectionState, set[ConnectionState]]] = {
        "created": {"starting", "stopping", "stopped", "failed"},
        "starting": {"connected", "stopping", "stopped", "failed"},
        "connected": {"stopping", "failed"},
        "stopping": {"stopped", "failed"},
        "failed": {"stopped"},
        "stopped": set(),
    }

    def __init__(self, request_handler: ServerRequestHandler | None = None) -> None:
        self._state: ConnectionState = "created"
        self._spawn_id: SpawnId = SpawnId("")
        self._config: ConnectionConfig | None = None
        self._launch_spec: ResolvedLaunchSpec | None = None
        self._request_handler = request_handler or AutoAcceptHandler()

        self._process: Process | None = None
        self._ws: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._stderr_handle: BufferedWriter | None = None
        self._stderr_log_path: Path | None = None
        self._stderr_read_offset = 0
        self._codex_home: Path | None = None
        self._scope_handle: ScopedProcessHandle | None = None
        self._parent_death_link: ParentDeathLink | None = None

        self._next_request_id = 1
        self._pending_requests: dict[int, asyncio.Future[dict[str, object]]] = {}
        self._hitl_requests: dict[str, object] = {}
        self._event_queue: asyncio.Queue[RawHarnessEvent | None] = asyncio.Queue()
        self._liveness = BackendLivenessPolicy(
            timeout_seconds=lambda: self._LIVENESS_TIMEOUT_SECONDS,
            now=lambda: _time.monotonic(),
            backend_pid=lambda: self._process.pid if self._process is not None else None,
            backend_birth_time=self._backend_birth_time,
        )

        self._current_turn_id: str | None = None
        self._thread_id: str | None = None
        self._tracer: DebugTracer | None = None
        self._cancel_requested = False
        self._signal_in_flight = False
        self._primary_observer_mode = False
        self._startup_emitter: StartupPhaseEmitter | None = None

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CODEX

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return ConnectionCapabilities(
            mid_turn_injection="interrupt_restart",
            supports_steer=True,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
            supports_primary_observer=True,
            supports_runtime_hitl=not getattr(self._request_handler, "no_runtime_hitl", True),
            supported_startup_phases=frozenset(
                phase.value
                for phase in (
                    StartupPhase.LAUNCHING_SUBPROCESS,
                    StartupPhase.WAITING_FOR_CONNECTION,
                    StartupPhase.INITIALIZING_SESSION,
                    StartupPhase.HARNESS_READY,
                    StartupPhase.HARNESS_FAILED,
                )
            ),
        )

    @property
    def session_id(self) -> str | None:
        return self._thread_id

    @property
    def current_turn_id(self) -> str | None:
        return self._current_turn_id

    @property
    def primary_event_scope(self) -> PrimaryEventScope | None:
        thread_id = (self._thread_id or "").strip()
        if not thread_id:
            return None
        return PrimaryEventScope(
            harness_id=HarnessId.CODEX,
            scope_id=thread_id,
            unscoped_events_match=True,
        )

    @property
    def subprocess_pid(self) -> int | None:
        process = self._process
        if process is None:
            return None
        return process.pid

    @property
    def observer_endpoint(self) -> ObserverEndpoint | None:
        if not self._primary_observer_mode:
            return None
        config = self._config
        if config is None:
            return None
        if config.ws_port <= 0:
            return None
        ws_url = f"ws://{config.ws_bind_host}:{config.ws_port}"
        return ObserverEndpoint(
            transport="ws",
            url=ws_url,
            host=config.ws_bind_host,
            port=config.ws_port,
        )

    @property
    def scope_snapshot(self) -> ProcessScopeSnapshot | None:
        handle = self._scope_handle
        if handle is None:
            return None
        return handle.snapshot

    def _backend_birth_time(self) -> float | None:
        snapshot = self.scope_snapshot
        if snapshot is None:
            return None
        return snapshot.root_created_at_epoch

    async def start(
        self,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> None:
        if self._state not in {"created", "stopped", "failed"}:
            raise RuntimeError(f"Cannot start CodexConnection from state '{self._state}'")

        validate_prompt_size(config)

        self._transition("starting")
        self._spawn_id = config.spawn_id
        self._startup_emitter = StartupPhaseEmitter(
            str(config.spawn_id),
            harness_id=config.harness_id.value,
            model=spec.model,
            agent=None,
        )
        self._tracer = config.debug_tracer
        self._config = config
        self._launch_spec = spec
        self._next_request_id = 1
        self._pending_requests = {}
        self._hitl_requests = {}
        self._event_queue = asyncio.Queue()
        self._current_turn_id = None
        self._thread_id = None
        self._liveness.reset()
        self._cancel_requested = False
        self._signal_in_flight = False

        host = config.ws_bind_host
        port = config.ws_port if config.ws_port > 0 else _reserve_port(host)
        ws_url = f"ws://{host}:{port}"

        env = inherit_child_env(os.environ, config.env_overrides)
        self._codex_home = resolve_codex_home(env)
        effective_cwd = self._effective_project_root()
        spawn_dir = resolve_spawn_log_dir(config.control_root, config.spawn_id)
        self._stderr_log_path = spawn_dir / "stderr.log"
        self._stderr_handle = self._stderr_log_path.open("ab")
        self._stderr_read_offset = self._stderr_handle.tell()

        try:
            appserver_command = project_managed_primary_backend_command(
                self.harness_id,
                spec,
                host=host,
                port=port,
            )
            self._emit_startup_phase(StartupPhase.LAUNCHING_SUBPROCESS)
            handle = await launch_managed_backend(
                ManagedBackendConfig(
                    spawn_id=config.spawn_id,
                    harness_id=self.harness_id,
                    command=tuple(appserver_command),
                    cwd=effective_cwd,
                    env=env,
                    control_root=config.control_root,
                ),
                stderr=self._stderr_handle,
            )
            self._process = handle.process
            self._scope_handle = handle.scope_handle
            self._parent_death_link = handle.parent_death_link

            self._emit_startup_phase(StartupPhase.WAITING_FOR_CONNECTION)
            self._ws = await self._connect_with_retry(
                ws_url,
                timeout_seconds=self._connect_timeout(),
            )
            self._reader_task = asyncio.create_task(self._read_messages_loop())

            await self._request(
                "initialize",
                {
                    "capabilities": {},
                    "clientInfo": {
                        "name": "meridian",
                        "version": __version__,
                    },
                },
                timeout_seconds=self._connect_timeout(),
            )
            await self._notify("initialized")

            if self._is_fresh_session(spec):
                self._emit_startup_phase(StartupPhase.INITIALIZING_SESSION)
            else:
                self._emit_startup_phase(StartupPhase.INITIALIZING_SESSION)
            thread_result = await self._bootstrap_thread(spec)
            self._thread_id = _extract_thread_id(thread_result)

            if not self._primary_observer_mode:
                await self._request(
                    "turn/start",
                    {
                        "threadId": self._require_thread_id("turn/start"),
                        "input": _build_text_user_input(config.prompt),
                    },
                )
            elif self._is_fresh_session(spec):
                # Fresh primary sessions need a bootstrap turn to materialize the
                # Codex rollout before the TUI can attach. Without this, Codex
                # cannot resume a fresh thread because no rollout file exists.
                self._emit_startup_phase(StartupPhase.INITIALIZING_SESSION)
                await self._send_bootstrap_turn_and_wait(agent_name=spec.agent_name)

            self._transition("connected")
            self._liveness.mark_activity()
            self._emit_startup_phase(StartupPhase.HARNESS_READY)
        except BaseException:
            self._emit_startup_phase(StartupPhase.HARNESS_FAILED)
            self._transition("failed")
            await reap_on_ownership_transfer_failure(self._cleanup_start_failure)
            raise

    async def _cleanup_start_failure(self) -> None:
        async with self._stop_lock:
            await self._cleanup_resources(mark_stopped=False)

    async def start_observer(
        self,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> None:
        """Start connection in primary observer mode."""

        self._primary_observer_mode = True
        await self.start(config, spec)

    def configure_primary_runtime_requests(
        self,
        *,
        policy: PrimaryRuntimeRequestPolicy,
        event_sink: Callable[[RawHarnessEvent], Awaitable[None]] | None = None,
        request_handler: ServerRequestHandler | None = None,
    ) -> None:
        if policy in (
            PrimaryRuntimeRequestPolicy.NONE,
            PrimaryRuntimeRequestPolicy.AUTO_ACCEPT,
        ):
            self._request_handler = AutoAcceptHandler()
            return
        if policy is PrimaryRuntimeRequestPolicy.SURFACE_EVENTS:
            if request_handler is not None:
                self._request_handler = request_handler
                return
            if event_sink is None:
                raise ValueError("Codex primary runtime event surfacing requires an event sink")
            self._request_handler = InteractiveHandler(event_sink)
            return
        raise ValueError(f"Unsupported Codex primary runtime request policy: {policy}")

    async def inject_runtime_event(self, event: RawHarnessEvent) -> None:
        await self._event_queue.put(event)

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        async with self._stop_lock:
            return await self._stop_unlocked()

    async def _stop_unlocked(self) -> StopResult:
        if self._state in {"stopped"}:
            return StopResult()
        self._primary_observer_mode = False

        if self._state != "failed":
            self._transition("stopping")

        await self._cleanup_resources(mark_stopped=self._state != "failed")
        return StopResult()

    @property
    def resident_backend(self) -> ResidentBackendControl:
        return LivenessResidentBackendControl(
            liveness=self._liveness,
            backend_dead=self._resident_backend_dead,
            begin_followup_turn=self._begin_followup_turn,
        )

    def health(self) -> bool:
        return not self._resident_backend_dead() and self._liveness.healthy

    async def send_user_message(self, text: str) -> None:
        self._require_connected("send_user_message")
        self._signal_in_flight = False

        if self._current_turn_id:
            await self._request(
                "turn/steer",
                {
                    "threadId": self._require_thread_id("turn/steer"),
                    "input": _build_text_user_input(text),
                    "expectedTurnId": self._current_turn_id,
                },
            )
            return

        await self._request(
            "turn/start",
            {
                "threadId": self._require_thread_id("turn/start"),
                "input": _build_text_user_input(text),
            },
        )

    async def _begin_followup_turn(self, message: str) -> None:
        self._require_connected("begin_followup_turn")
        if self._current_turn_id is not None:
            raise ConnectionNotReady("Codex follow-up turns require an idle backend")
        self._signal_in_flight = False
        await self._request(
            "turn/start",
            {
                "threadId": self._require_thread_id("begin_followup_turn"),
                "input": _build_text_user_input(message),
            },
        )

    async def send_cancel(self) -> None:
        if self._cancel_requested:
            return
        if self._state in {"stopping", "stopped", "failed"}:
            self._cancel_requested = True
            return
        self._require_connected("send_cancel")

        self._cancel_requested = True
        self._signal_in_flight = True
        self._liveness.signal_request_in_flight("cancel")
        self._transition("stopping")
        await self._close_ws()

    async def events(self) -> AsyncIterator[RawHarnessEvent]:
        while True:
            event = await self._event_queue.get()
            if event is None:
                return
            yield event

    async def respond_request(
        self,
        request_id: str,
        decision: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        jsonrpc_id = self._hitl_requests.get(request_id)
        if jsonrpc_id is None:
            raise ValueError(f"No pending Codex HITL request: {request_id}")
        result: dict[str, object] = {"decision": decision}
        if payload:
            result.update(payload)
        try:
            await self._send_jsonrpc_result(jsonrpc_id, result)
        except Exception:
            raise
        self._hitl_requests.pop(request_id, None)
        await self._notify_request_resolved(
            request_id,
            resolution={"decision": decision, **(payload or {})},
        )

    async def respond_user_input(
        self,
        request_id: str,
        answers: dict[str, object],
    ) -> None:
        jsonrpc_id = self._hitl_requests.get(request_id)
        if jsonrpc_id is None:
            raise ValueError(f"No pending Codex HITL request: {request_id}")
        try:
            await self._send_jsonrpc_result(jsonrpc_id, {"answers": answers})
        except Exception:
            raise
        self._hitl_requests.pop(request_id, None)
        await self._notify_request_resolved(
            request_id,
            resolution={"answers": answers},
        )

    async def _connect_with_retry(self, ws_url: str, timeout_seconds: float) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        last_error: Exception | None = None

        while loop.time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                raise self._startup_exit_exception()

            try:
                if _WEBSOCKETS_MODULE is not None:
                    return await _WEBSOCKETS_MODULE.connect(
                        ws_url,
                        max_size=MAX_HARNESS_MESSAGE_BYTES,
                    )
                return await _aiohttp_connect(ws_url)
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.1)

        raise TimeoutError(f"Timed out connecting to Codex websocket at {ws_url}") from last_error

    async def _request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        request_id = self._next_request_id
        self._next_request_id += 1

        ws = self._ws
        if ws is None:
            raise RuntimeError("Codex websocket is not connected")

        payload: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[dict[str, object]] = loop.create_future()
        self._pending_requests[request_id] = response_future
        request_key = f"rpc:{request_id}"

        trace_wire_send(
            self._tracer,
            "ws_send_request",
            json.dumps(payload),
            method=method,
            request_id=request_id,
        )
        self._liveness.signal_request_in_flight(request_key)
        try:
            await self._send_json(payload)
            response = await asyncio.wait_for(
                response_future,
                timeout=timeout_seconds or _DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )
        finally:
            self._liveness.signal_request_resolved(request_key)
            self._pending_requests.pop(request_id, None)

        error_obj = response.get("error")
        if isinstance(error_obj, dict):
            error_payload = cast("dict[str, object]", error_obj)
            code = error_payload.get("code")
            message = error_payload.get("message")
            raise RuntimeError(f"Codex JSON-RPC error for {method}: code={code} message={message}")

        result_obj = response.get("result")
        result = cast("dict[str, object]", result_obj) if isinstance(result_obj, dict) else {}
        self._update_turn_state(method=method, payload=result)
        return result

    async def _notify(self, method: str, params: dict[str, object] | None = None) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("Codex websocket is not connected")

        payload: dict[str, object] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        trace_wire_send(self._tracer, "ws_send_notify", json.dumps(payload), method=method)
        await self._send_json(payload)

    async def _read_messages_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return

        try:
            ws_iter = ws.__aiter__()
            while True:
                try:
                    raw_message = await self._liveness.wait_for_activity(ws_iter.__anext__())
                except StopAsyncIteration:
                    return
                except EventStreamLivenessTimeout:
                    message = (
                        "Codex event stream liveness timeout after "
                        f"{self._LIVENESS_TIMEOUT_SECONDS:.1f}s without events"
                    )
                    logger.warning(message)
                    if self._state in {"starting", "connected"}:
                        self._emit_startup_phase(StartupPhase.HARNESS_FAILED)
                        self._transition("failed")
                        await self._event_queue.put(
                            RawHarnessEvent(
                                event_type="meridian/error/connectionClosed",
                                payload={"message": message},
                                harness_id=self.harness_id.value,
                                raw_text=None,
                            )
                        )
                    return
                except Exception as exc:
                    if _is_clean_websocket_close(exc):
                        return
                    raise
                self._liveness.mark_activity()
                raw_text = _coerce_text(raw_message)
                if self._tracer is not None:
                    self._tracer.emit(
                        "wire",
                        "ws_recv",
                        direction="inbound",
                        data={"raw_text": raw_text, "bytes": len(raw_text.encode("utf-8"))},
                    )
                parsed = _parse_jsonrpc(raw_text)
                if parsed is None:
                    trace_parse_error(self._tracer, "codex", raw_text, error="malformed_json_rpc")
                    continue

                method = parsed.get("method")
                if "id" in parsed and isinstance(method, str):
                    await self._handle_server_request(parsed)
                    continue

                if "id" in parsed:
                    response_id = _coerce_int(parsed.get("id"))
                    if response_id is None:
                        continue
                    if self._tracer is not None:
                        self._tracer.emit(
                            "wire",
                            "ws_recv_response",
                            direction="inbound",
                            data={"request_id": response_id, "has_error": "error" in parsed},
                        )
                    future = self._pending_requests.get(response_id)
                    if future is not None and not future.done():
                        future.set_result(parsed)
                    continue

                if not isinstance(method, str):
                    continue

                if self._tracer is not None:
                    self._tracer.emit(
                        "wire",
                        "ws_recv_notification",
                        direction="inbound",
                        data={"method": method},
                    )

                params_obj = parsed.get("params")
                payload = (
                    cast("dict[str, object]", params_obj) if isinstance(params_obj, dict) else {}
                )
                await self._handle_notification(
                    method=method,
                    payload=payload,
                    raw_text=raw_text,
                )
        except Exception as exc:
            if self._state in {"starting", "connected"}:
                self._emit_startup_phase(StartupPhase.HARNESS_FAILED)
                self._transition("failed")
                await self._event_queue.put(
                    RawHarnessEvent(
                        event_type="meridian/error/connectionClosed",
                        payload={"message": str(exc)},
                        harness_id=self.harness_id.value,
                        raw_text=None,
                    )
                )
        finally:
            self._fail_pending_requests(RuntimeError("Codex websocket closed"))
            await self._clear_stale_hitl_requests(reason="websocket_closed")
            await self._event_queue.put(None)

    async def _cleanup_resources(self, *, mark_stopped: bool) -> None:
        await self._close_ws()

        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None

        scope_handle = self._scope_handle
        self._scope_handle = None
        process = self._process
        self._process = None

        if scope_handle is not None and process is not None and process.returncode is None:
            await scope_handle.terminate(
                grace_seconds=_STOP_WAIT_TIMEOUT_SECONDS,
                reason="stop_called",
            )
        elif process is not None and process.returncode is None:
            # Legacy fallback when scope handle was never created (e.g. start()
            # failed before subprocess was launched).
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_STOP_WAIT_TIMEOUT_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()

        await asyncio.to_thread(release_parent_death_link, self._parent_death_link)
        self._parent_death_link = None
        self._fail_pending_requests(RuntimeError("Codex connection stopped"))
        await self._clear_stale_hitl_requests(reason="connection_stopped")
        self._clear_turn_liveness_signals()
        self._thread_id = None
        self._cancel_requested = False
        self._signal_in_flight = False
        self._liveness.signal_request_resolved("cancel")
        self._launch_spec = None
        self._codex_home = None
        self._startup_emitter = None
        self._close_log_handles()

        if mark_stopped:
            self._transition("stopped")
            await self._event_queue.put(None)

    async def _close_ws(self) -> None:
        ws = self._ws
        if ws is None:
            return

        self._ws = None
        with contextlib.suppress(Exception):
            await ws.close()

    async def _send_json(self, payload: dict[str, object]) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("Codex websocket is not connected")
        async with self._send_lock:
            await ws.send(json.dumps(payload))

    async def _send_jsonrpc_result(self, request_id: object, result: dict[str, object]) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }
        trace_wire_send(
            self._tracer,
            "ws_send_result",
            json.dumps(payload),
            request_id=request_id,
        )
        await self._send_json(payload)

    async def _send_jsonrpc_error(
        self,
        request_id: object,
        *,
        code: int,
        message: str,
    ) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message,
            },
        }
        trace_wire_send(
            self._tracer,
            "ws_send_error",
            json.dumps(payload),
            request_id=request_id,
            error_code=code,
        )
        await self._send_json(payload)

    async def _handle_server_request(self, message: dict[str, object]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params_obj = message.get("params")
        payload = cast("dict[str, object]", params_obj) if isinstance(params_obj, dict) else {}

        if not isinstance(method, str):
            return

        if self._tracer is not None:
            self._tracer.emit(
                "wire",
                "ws_recv_server_request",
                direction="inbound",
                data={"method": method},
            )

        if method.endswith("/requestApproval"):
            if await self._reject_confirm_mode_approval_request(request_id, method):
                return
            harness_request = HarnessRequest(
                request_id=str(request_id),
                request_type="approval",
                method=method,
                payload=payload,
            )
            self._hitl_requests[harness_request.request_id] = request_id
            await self._request_handler.handle_request(self, harness_request)
            return

        if method == "item/tool/requestUserInput":
            harness_request = HarnessRequest(
                request_id=str(request_id),
                request_type="user_input",
                method=method,
                payload=payload,
            )
            self._hitl_requests[harness_request.request_id] = request_id
            await self._request_handler.handle_request(self, harness_request)
            return

        await self._event_queue.put(
            RawHarnessEvent(
                event_type="warning/unsupportedServerRequest",
                payload={"method": method, "params": payload},
                harness_id=self.harness_id.value,
                raw_text=None,
            )
        )
        await self._send_jsonrpc_error(
            request_id,
            code=-32601,
            message=f"Meridian codex_ws adapter does not support server request '{method}'",
        )

    async def _reject_confirm_mode_approval_request(
        self,
        request_id: object,
        method: str,
    ) -> bool:
        launch_spec = self._launch_spec
        if launch_spec is None or launch_spec.permission_resolver.config.approval != "confirm":
            return False
        if not getattr(self._request_handler, "no_runtime_hitl", False):
            return False

        logger.warning(
            "Rejecting Codex server approval request in confirm mode: %s",
            method,
        )
        await self._event_queue.put(
            RawHarnessEvent(
                event_type="warning/approvalRejected",
                payload={
                    "reason": "confirm_mode",
                    "method": method,
                },
                harness_id=self.harness_id.value,
                raw_text=None,
            )
        )
        await self._send_jsonrpc_error(
            request_id,
            code=-32000,
            message="Codex websocket approval requests are unsupported in confirm mode.",
        )
        return True

    async def _handle_notification(
        self,
        *,
        method: str,
        payload: dict[str, object],
        raw_text: str,
    ) -> None:
        if method == "serverRequest/resolved":
            await self._resolve_server_request(payload)
        self._update_turn_state(method=method, payload=payload)
        await self._event_queue.put(
            RawHarnessEvent(
                event_type=method,
                payload=payload,
                harness_id=self.harness_id.value,
                raw_text=raw_text,
            )
        )

    async def _resolve_server_request(self, payload: dict[str, object]) -> None:
        resolved_request_id = _extract_server_request_id(payload)
        if resolved_request_id is None:
            return
        if self._hitl_requests.pop(resolved_request_id, None) is None:
            return
        await self._notify_request_resolved(
            resolved_request_id,
            resolution={"source": "serverRequest/resolved"},
        )

    async def _clear_stale_hitl_requests(self, *, reason: str) -> None:
        pending_count = len(self._hitl_requests)
        if pending_count == 0:
            return
        stale_ids = list(self._hitl_requests.keys())
        logger.debug(
            "Dropping %d stale Codex HITL request(s) during %s",
            pending_count,
            reason,
        )
        self._hitl_requests = {}
        await self._notify_requests_cancelled(stale_ids, reason=reason)

    async def _notify_request_resolved(
        self,
        request_id: str,
        *,
        resolution: dict[str, object] | None = None,
    ) -> None:
        callback = getattr(self._request_handler, "on_request_resolved", None)
        if callback is None:
            return
        try:
            await cast(
                "Callable[..., Awaitable[None]]",
                callback,
            )(request_id, resolution=resolution)
        except Exception:
            logger.warning(
                "Codex request handler failed to persist resolved state for request %s",
                request_id,
                exc_info=True,
            )

    async def _notify_request_failed(self, request_id: str, *, error: str) -> None:
        callback = getattr(self._request_handler, "on_request_failed", None)
        if callback is None:
            return
        try:
            await cast("Callable[..., Awaitable[None]]", callback)(request_id, error=error)
        except Exception:
            logger.warning(
                "Codex request handler failed to persist failure state for request %s",
                request_id,
                exc_info=True,
            )

    async def _notify_requests_cancelled(self, request_ids: list[str], *, reason: str) -> None:
        if not request_ids:
            return
        callback = getattr(self._request_handler, "on_requests_cancelled", None)
        if callback is None:
            return
        try:
            await cast("Callable[..., Awaitable[None]]", callback)(request_ids, reason=reason)
        except Exception:
            logger.warning(
                "Codex request handler failed to persist cancelled request state",
                exc_info=True,
            )

    def _fail_pending_requests(self, error: Exception) -> None:
        for request_id in list(self._pending_requests.keys()):
            future = self._pending_requests.pop(request_id)
            if not future.done():
                future.set_exception(error)

    def _close_log_handles(self) -> None:
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None
        self._stderr_log_path = None
        self._stderr_read_offset = 0

    def _startup_exit_exception(self) -> Exception:
        process = self._process
        exit_code = process.returncode if process is not None else None
        stderr_excerpt = self._read_startup_stderr_excerpt()
        if _looks_like_address_in_use(stderr_excerpt):
            return PortBindError(
                "Codex app-server failed to bind websocket port "
                f"(exit={exit_code}): {stderr_excerpt or '<no stderr>'}"
            )
        if stderr_excerpt:
            return RuntimeError(
                "Codex app-server exited before websocket connect "
                f"(exit={exit_code}): {stderr_excerpt}"
            )
        return RuntimeError(f"Codex app-server exited before websocket connect (exit={exit_code})")

    def _read_startup_stderr_excerpt(self) -> str:
        stderr_handle = self._stderr_handle
        if stderr_handle is not None:
            stderr_handle.flush()

        path = self._stderr_log_path
        if path is None or not path.exists():
            return ""

        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end_offset = handle.tell()
            start_offset = min(self._stderr_read_offset, end_offset)
            read_offset = max(start_offset, end_offset - _STARTUP_STDERR_MAX_BYTES)
            handle.seek(read_offset, os.SEEK_SET)
            data = handle.read(max(0, end_offset - read_offset))
        return data.decode("utf-8", errors="replace").strip()

    def _resident_backend_dead(self) -> bool:
        process_running = self._process is not None and self._process.returncode is None
        ws_open = self._ws is not None and _ws_is_open(self._ws)
        return self._state != "connected" or not process_running or not ws_open

    def _transition(self, next_state: ConnectionState) -> None:
        """Validate and apply a state transition."""
        if next_state == self._state:
            return
        allowed = self._ALLOWED_TRANSITIONS.get(self._state, set())
        if next_state not in allowed:
            raise RuntimeError(
                f"Invalid CodexConnection transition: {self._state!r} -> {next_state!r}"
            )
        trace_state_change(self._tracer, "codex", self._state, next_state)
        self._state = next_state

    def _require_connected(self, operation: str) -> None:
        if self._state != "connected":
            raise ConnectionNotReady(
                f"Codex connection is not connected; cannot {operation} from state '{self._state}'"
            )

    def _require_thread_id(self, operation: str) -> str:
        thread_id = self._thread_id
        if thread_id is None:
            raise ConnectionNotReady(f"Codex thread ID is unavailable; cannot {operation}")
        return thread_id

    def _connect_timeout(self) -> float:
        timeout_seconds = self._config.timeout_seconds if self._config is not None else None
        if timeout_seconds is not None and timeout_seconds > 0:
            return timeout_seconds
        return _DEFAULT_CONNECT_TIMEOUT_SECONDS

    def _emit_startup_phase(self, phase: StartupPhase) -> None:
        emitter = self._startup_emitter
        if emitter is None:
            return
        emitter.emit(phase)

    def _is_fresh_session(self, spec: ResolvedLaunchSpec) -> bool:
        """Check if this is a fresh session (not resume or fork)."""
        return not (spec.continue_session_id or "").strip()

    def _effective_project_root(self) -> Path:
        """Project root Codex should bind the app-server/thread/rollout to."""

        config = self._config
        if config is None:
            raise RuntimeError("Codex connection config is unavailable")
        return config.control_root

    async def _send_bootstrap_turn_and_wait(self, *, agent_name: str | None) -> None:
        """Send a bootstrap turn and wait for the rollout to become attachable.

        This materializes the Codex rollout file so the TUI can later attach
        to this thread. The bootstrap prompt is deterministic and minimal.

        The load-bearing requirement is "thread is attachable", not "bootstrap
        response is semantically complete". We gate on rollout materialization
        because that is the earliest observed durable signal that
        ``codex resume --remote`` can attach. We intentionally do NOT wait for
        ``turn/completed`` — the model may begin real work (reading context,
        calling tools) and take arbitrarily long. The TUI handles in-progress
        turns fine.
        """
        thread_id = self._require_thread_id("bootstrap_turn")
        normalized_agent = (agent_name or "").strip()
        prompt = _BOOTSTRAP_TURN_PROMPT
        if normalized_agent:
            prompt = f"{prompt} (agent: {normalized_agent})"
        await self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": _build_text_user_input(prompt),
            },
        )
        await self._wait_for_rollout_materialization()

    async def _wait_for_rollout_materialization(self, timeout_seconds: float = 120.0) -> None:
        """Block until Codex has created an attachable rollout for the thread.

        The attach-safe filesystem signal is a rollout file whose session_meta
        exists and whose cwd matches this project.
        """
        config = self._config
        codex_home = self._codex_home
        thread_id = self._require_thread_id("rollout_materialization")
        if config is None or codex_home is None:
            raise RuntimeError("Codex connection config is unavailable for rollout wait")

        poll_interval = 0.1
        elapsed = 0.0
        while True:
            if (
                find_attachable_rollout_session_id(
                    codex_home=codex_home,
                    project_root=self._effective_project_root(),
                    session_id=thread_id,
                )
                == thread_id
            ):
                return

            if elapsed >= timeout_seconds:
                raise RuntimeError(
                    "Bootstrap turn timed out after "
                    f"{timeout_seconds}s waiting for Codex rollout materialization"
                )
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            process_running = self._process is not None and self._process.returncode is None
            ws_open = self._ws is not None and _ws_is_open(self._ws)
            if not process_running or not ws_open:
                raise RuntimeError("Codex connection lost during bootstrap turn")

    async def _bootstrap_thread(self, spec: ResolvedLaunchSpec) -> dict[str, object]:
        method, payload = self._thread_bootstrap_request(spec)
        return await self._request(method, payload)

    def _thread_bootstrap_request(
        self,
        spec: ResolvedLaunchSpec,
    ) -> tuple[str, dict[str, object]]:
        config = self._config
        if config is None:
            raise RuntimeError("Codex connection config is unavailable for thread bootstrap")
        projected = project_managed_primary_bootstrap(
            self.harness_id,
            spec,
            project_root=self._effective_project_root(),
        )
        if not isinstance(projected, tuple):
            raise TypeError("Codex managed-primary bootstrap must be (method, payload)")
        projected_tuple = cast("tuple[object, ...]", projected)
        if len(projected_tuple) != 2:
            raise TypeError("Codex managed-primary bootstrap must be (method, payload)")
        method_obj, payload_obj = projected_tuple
        if not isinstance(method_obj, str) or not isinstance(payload_obj, dict):
            raise TypeError("Codex managed-primary bootstrap returned invalid types")
        return method_obj, cast("dict[str, object]", payload_obj)

    def _update_turn_state(self, *, method: str, payload: dict[str, object]) -> None:
        if method in {"thread/start", "thread/started"}:
            thread_id = _extract_thread_id(payload)
            if thread_id is not None:
                self._thread_id = thread_id

        turn_id = _extract_turn_id(payload)
        if turn_id is not None:
            if self._current_turn_id is not None and self._current_turn_id != turn_id:
                self._liveness.signal_turn_ended(self._current_turn_id)
            self._current_turn_id = turn_id
            self._liveness.signal_turn_started(turn_id)
            self._signal_in_flight = False
            self._liveness.signal_request_resolved("cancel")

    def observe_event_semantics(self, semantics: EventSemantics) -> None:
        if semantics.clears_signal:
            self._end_current_turn()
            self._signal_in_flight = False
            self._liveness.signal_request_resolved("cancel")

    def _end_current_turn(self) -> None:
        if self._current_turn_id is not None:
            self._liveness.signal_turn_ended(self._current_turn_id)
        self._current_turn_id = None

    def _clear_turn_liveness_signals(self) -> None:
        self._end_current_turn()


def _reserve_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _looks_like_address_in_use(stderr_text: str) -> bool:
    normalized = stderr_text.lower()
    return any(marker in normalized for marker in _ADDRESS_IN_USE_MARKERS)


def _coerce_text(raw_message: object) -> str:
    if isinstance(raw_message, bytes):
        return raw_message.decode("utf-8", errors="replace")
    return str(raw_message)


def _build_text_user_input(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


def _extract_thread_id(payload: dict[str, object]) -> str | None:
    thread_id = _extract_str(payload, "threadId")
    if thread_id is not None:
        return thread_id
    thread_obj = payload.get("thread")
    if isinstance(thread_obj, dict):
        return _extract_str(cast("dict[str, object]", thread_obj), "id")
    return None


def _extract_turn_id(payload: dict[str, object]) -> str | None:
    turn_id = _extract_str(payload, "turnId")
    if turn_id is not None:
        return turn_id
    turn_obj = payload.get("turn")
    if isinstance(turn_obj, dict):
        return _extract_str(cast("dict[str, object]", turn_obj), "id")
    return None


def _extract_server_request_id(payload: dict[str, object]) -> str | None:
    for key in ("request_id", "requestId", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        if isinstance(value, int):
            return str(value)

    request_obj = payload.get("request")
    if isinstance(request_obj, dict):
        nested_id = _extract_server_request_id(cast("dict[str, object]", request_obj))
        if nested_id is not None:
            return nested_id

    return None


def _parse_jsonrpc(raw_text: str) -> dict[str, object] | None:
    try:
        parsed_obj = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed_obj, dict):
        return None
    return cast("dict[str, object]", parsed_obj)


def _coerce_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _extract_str(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


__all__ = ["CodexConnection"]
