"""HTTP-backed bidirectional OpenCode harness connection."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import socket
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from io import BufferedWriter
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast
from urllib.parse import urlparse

if TYPE_CHECKING:
    from meridian.lib.observability.debug_tracer import DebugTracer

from meridian.lib.core.telemetry import StartupPhase, StartupPhaseEmitter
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.bundle import (
    project_managed_primary_backend_command,
    project_managed_primary_bootstrap,
)
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionNotReady,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    ObserverEndpoint,
    StopProgressCallback,
    StopResult,
    validate_prompt_size,
)
from meridian.lib.harness.connections.errors import PortBindError
from meridian.lib.harness.connections.liveness import (
    BackendLivenessPolicy,
    EventStreamLivenessTimeout,
    LivenessDecision,
)
from meridian.lib.harness.connections.managed_backend import (
    ManagedBackendConfig,
    launch_managed_backend,
)
from meridian.lib.harness.connections.resident_backend import (
    LivenessResidentBackendControl,
    ResidentBackendControl,
)
from meridian.lib.harness.projections.project_opencode_streaming import (
    project_opencode_spec_to_session_payload as _project_opencode_spec_to_session_payload,
)
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.harness.semantics import (
    PrimaryEventScope,
    clears_signal,
    opencode_primary_event_scope,
)
from meridian.lib.launch.env import inherit_child_env
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.workspace_projection import OPENCODE_CONFIG_CONTENT_ENV
from meridian.lib.observability.trace_helpers import (
    trace_parse_error,
    trace_state_change,
    trace_wire_recv,
    trace_wire_send,
)
from meridian.lib.platform.detached_process import ParentDeathLink
from meridian.lib.platform.process_scope import (
    ProcessScopeSnapshot,
    ScopedProcessHandle,
)
from meridian.lib.state.paths import resolve_spawn_log_dir

logger = logging.getLogger(__name__)
_STARTUP_STDERR_MAX_BYTES = 16 * 1024
_ADDRESS_IN_USE_MARKERS = ("address already in use", "address in use", "eaddrinuse")


class SessionNotReadyError(RuntimeError):
    """Retryable OpenCode session readiness failure."""


def project_opencode_spec_to_serve_command(
    spec: ResolvedLaunchSpec,
    *,
    host: str,
    port: int,
) -> list[str]:
    """Project OpenCode managed-primary backend command through the contract seam."""

    return project_managed_primary_backend_command(
        HarnessId.OPENCODE,
        spec,
        host=host,
        port=port,
    )


def project_opencode_spec_to_session_payload(
    spec: ResolvedLaunchSpec,
    *,
    project_root: Path | None = None,
) -> dict[str, object]:
    """Project OpenCode managed-primary session payload through the contract seam."""

    if project_root is None:
        return _project_opencode_spec_to_session_payload(spec)

    projected = project_managed_primary_bootstrap(
        HarnessId.OPENCODE,
        spec,
        project_root=project_root,
    )
    if not isinstance(projected, dict):
        raise TypeError("OpenCode managed-primary bootstrap must be a dict payload")
    return cast("dict[str, object]", projected)


class OpenCodeConnection(HarnessConnection[ResolvedLaunchSpec]):
    """Bidirectional OpenCode connection over the OpenCode HTTP API."""

    _CAPABILITIES: ClassVar[ConnectionCapabilities] = ConnectionCapabilities(
        mid_turn_injection="http_post",
        supports_steer=False,
        supports_cancel=True,
        runtime_model_switch=False,
        structured_reasoning=True,
        supports_primary_observer=True,
        supported_startup_phases=frozenset(
            phase.value
            for phase in (
                StartupPhase.WAITING_FOR_CONNECTION,
                StartupPhase.INITIALIZING_SESSION,
                StartupPhase.HARNESS_READY,
            )
        ),
    )
    _STATE_TRANSITIONS: ClassVar[dict[ConnectionState, frozenset[ConnectionState]]] = {
        "created": frozenset(("starting", "stopping", "failed")),
        "starting": frozenset(("connected", "stopping", "failed")),
        "connected": frozenset(("stopping", "failed")),
        "stopping": frozenset(("stopped", "failed")),
        "stopped": frozenset(("starting",)),
        "failed": frozenset(("starting", "stopping", "stopped")),
    }
    _HEALTH_PATHS: ClassVar[tuple[str, ...]] = ("/global/health",)
    _CREATE_SESSION_PATHS: ClassVar[tuple[str, ...]] = ("/session",)
    _MESSAGE_PATH_TEMPLATES: ClassVar[tuple[str, ...]] = (
        "/session/{session_id}/prompt_async",
        "/session/{session_id}/message",
    )
    _EVENT_PATHS: ClassVar[tuple[str, ...]] = (
        "/global/event",
        "/event",
    )
    _CANCEL_PATH_TEMPLATES: ClassVar[tuple[str, ...]] = ("/session/{session_id}/abort",)
    _PATH_RETRY_STATUSES: ClassVar[frozenset[int]] = frozenset((404, 405))
    _PAYLOAD_RETRY_STATUSES: ClassVar[frozenset[int]] = frozenset((400, 415, 422))
    _SUCCESS_STATUSES: ClassVar[frozenset[int]] = frozenset((200, 201, 202, 204))
    _ACTION_SUCCESS_STATUSES: ClassVar[frozenset[int]] = frozenset((200, 201, 202, 204, 409))
    _EVENT_RETRY_DELAY_SECONDS: ClassVar[float] = 0.25
    _LIVENESS_TIMEOUT_SECONDS: ClassVar[float] = 120.0
    _STARTUP_TIMEOUT_SECONDS: ClassVar[float] = 90.0
    _READY_TIMEOUT_SECONDS: ClassVar[float] = 60.0
    _SESSION_STARTUP_TIMEOUT_SECONDS: ClassVar[float] = (
        _STARTUP_TIMEOUT_SECONDS - _READY_TIMEOUT_SECONDS
    )
    _SESSION_CREATE_PAYLOAD_TIMEOUT_SECONDS: ClassVar[float] = 5.0
    # Per-attempt cap for startup probes. `opencode serve` accepts the TCP
    # connection before its HTTP handler is ready, so the first GET after the
    # socket starts accepting can hang. Bounding each probe (instead of the whole
    # remaining budget) lets a hung request be abandoned and retried; aiohttp's
    # own ClientTimeout tears down the stuck connection so the retry reconnects.
    _PROBE_TIMEOUT_SECONDS: ClassVar[float] = 4.0
    _STOP_GRACE_SECONDS: ClassVar[float] = 5.0
    _EVENT_ACCEPT_HEADER: ClassVar[dict[str, str]] = {"Accept": "text/event-stream"}

    def __init__(self) -> None:
        self._state: ConnectionState = "created"
        self._spawn_id: SpawnId | None = None
        self._config: ConnectionConfig | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._client: Any | None = None
        self._aiohttp_module: Any | None = None
        self._stderr_handle: BufferedWriter | None = None
        self._stderr_log_path: Path | None = None
        self._stderr_read_offset = 0
        self._base_url: str | None = None
        self._session_id: str | None = None
        self._event_path: str | None = None
        self._last_health_ok = False
        self._liveness = BackendLivenessPolicy(
            timeout_seconds=lambda: self._LIVENESS_TIMEOUT_SECONDS,
            now=lambda: time.monotonic(),
            backend_pid=lambda: self._process.pid if self._process is not None else None,
            backend_birth_time=self._backend_birth_time,
        )
        self._tracer: DebugTracer | None = None
        self._cancel_requested = False
        self._signal_in_flight = False
        self._primary_observer_mode = False
        self._startup_emitter: StartupPhaseEmitter | None = None
        self._scope_handle: ScopedProcessHandle | None = None
        self._parent_death_link: ParentDeathLink | None = None

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.OPENCODE

    @property
    def spawn_id(self) -> SpawnId:
        if self._spawn_id is None:
            raise RuntimeError("OpenCode connection has not been started")
        return self._spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return self._CAPABILITIES

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def primary_event_scope(self) -> PrimaryEventScope | None:
        return opencode_primary_event_scope(self._session_id)

    @property
    def subprocess_pid(self) -> int | None:
        process = self._process
        if process is None:
            return None
        return process.pid

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

    @property
    def observer_endpoint(self) -> ObserverEndpoint | None:
        if not self._primary_observer_mode:
            return None
        base_url = self._base_url
        if base_url is None:
            return None
        parsed = urlparse(base_url)
        return ObserverEndpoint(
            transport="http",
            url=base_url,
            host=parsed.hostname,
            port=parsed.port,
        )

    async def start(
        self,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> None:
        if self._state not in {"created", "stopped", "failed"}:
            raise RuntimeError(f"Cannot start OpenCode connection from state '{self._state}'")

        validate_prompt_size(config)

        self._config = config
        self._spawn_id = config.spawn_id
        self._tracer = config.debug_tracer
        self._liveness.reset()
        self._startup_emitter = StartupPhaseEmitter(
            str(config.spawn_id),
            harness_id=config.harness_id.value,
            model=spec.model,
            agent=spec.agent_name,
        )
        self._cancel_requested = False
        self._signal_in_flight = False
        self._transition("starting")

        readiness_timeout, session_timeout = self._startup_timeout_budgets(
            config.timeout_seconds
        )

        try:
            await self._launch_process(config, spec)
            self._emit_startup_phase(StartupPhase.WAITING_FOR_CONNECTION)
            await self._wait_for_ready(timeout_seconds=readiness_timeout)
            self._session_id = await self._create_session_with_retry(
                spec,
                timeout_seconds=session_timeout,
            )
            if config.session_id_observer is not None:
                config.session_id_observer(self._session_id)
            if not self._primary_observer_mode:
                await self._post_session_message(config.prompt, system=config.system)
        except Exception:
            self._set_failed()
            await self._cleanup_runtime()
            raise

        self._transition("connected")
        self._emit_startup_phase(StartupPhase.HARNESS_READY)
        self._last_health_ok = True

    async def start_observer(
        self,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> None:
        """Start connection in primary observer mode."""

        self._primary_observer_mode = True
        await self.start(config, spec)

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        if self._state == "stopped":
            return StopResult()
        self._primary_observer_mode = False
        if self._state != "stopping":
            self._transition("stopping")

        await self._cleanup_runtime()
        self._cancel_requested = False
        self._signal_in_flight = False
        self._transition("stopped")
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
        self._require_connected()
        self._signal_in_flight = False
        await self._post_session_message(text)

    async def _begin_followup_turn(self, message: str) -> None:
        self._require_connected()
        if self._signal_in_flight:
            raise ConnectionNotReady("OpenCode follow-up turns require an idle backend")
        self._signal_in_flight = False
        await self._post_session_message(message)

    async def send_cancel(self) -> None:
        if self._cancel_requested:
            return
        if self._state in {"stopping", "stopped", "failed"}:
            self._cancel_requested = True
            return
        self._require_connected()
        self._cancel_requested = True
        self._signal_in_flight = True
        self._liveness.signal_request_in_flight("cancel")
        self._transition("stopping")
        await self._post_session_action(
            path_templates=self._CANCEL_PATH_TEMPLATES,
            payload_variants=(
                {"response": "abort"},
                {"reason": "cancel"},
                {"type": "cancel"},
                {},
            ),
            accepted_statuses=self._ACTION_SUCCESS_STATUSES,
        )

    async def events(self) -> AsyncIterator[HarnessEvent]:
        if self._state not in ("connected", "stopping"):
            return
        if self._session_id is None:
            return

        sse_event_type: str | None = None
        sse_data_lines: list[str] = []

        while self._state in ("connected", "stopping"):
            if self._liveness.evaluate() in (
                LivenessDecision.BACKEND_DEAD,
                LivenessDecision.STREAM_STALLED,
            ):
                logger.warning(
                    "OpenCode event stream liveness timeout after %.1fs without events",
                    self._LIVENESS_TIMEOUT_SECONDS,
                )
                self._set_failed()
                return
            if self._process_exited():
                self._set_failed()
                return

            self._liveness.mark_activity_if_idle()
            try:
                response = await self._liveness.wait_for_activity(self._open_event_stream())
            except EventStreamLivenessTimeout:
                logger.warning(
                    "OpenCode event stream liveness timeout after %.1fs without events",
                    self._LIVENESS_TIMEOUT_SECONDS,
                )
                self._set_failed()
                return
            except Exception as exc:
                if self._state in ("stopping", "stopped"):
                    return
                if self._process_exited():
                    self._set_failed()
                    return
                logger.warning("OpenCode event stream dropped; reconnecting: %s", exc)
                await asyncio.sleep(self._EVENT_RETRY_DELAY_SECONDS)
                continue

            buffer = ""
            try:
                while self._state not in ("stopping", "stopped", "failed"):
                    try:
                        chunk = await self._liveness.wait_for_activity(
                            response.content.read(4096)
                        )
                    except EventStreamLivenessTimeout:
                        logger.warning(
                            "OpenCode event stream liveness timeout after %.1fs without events",
                            self._LIVENESS_TIMEOUT_SECONDS,
                        )
                        self._set_failed()
                        return
                    if not chunk:
                        break
                    buffer += chunk.decode("utf-8", errors="replace")
                    while True:
                        newline_index = buffer.find("\n")
                        if newline_index < 0:
                            break
                        raw_line = buffer[:newline_index]
                        buffer = buffer[newline_index + 1 :]
                        event, sse_event_type = self._consume_stream_line(
                            raw_line.rstrip("\r"),
                            sse_event_type=sse_event_type,
                            sse_data_lines=sse_data_lines,
                        )
                        if event is not None:
                            if self._tracer is not None:
                                self._tracer.emit(
                                    "wire",
                                    "sse_event",
                                    direction="inbound",
                                    data={"event_type": event.event_type},
                                )
                            self._liveness.mark_activity()
                            yield event

                if buffer.strip():
                    event = self._event_from_json_line(buffer.strip(), raw_text=buffer.strip())
                    if event is not None:
                        self._liveness.mark_activity()
                        yield event
                final_sse_event = self._flush_sse_event(
                    sse_event_type=sse_event_type,
                    sse_data_lines=sse_data_lines,
                )
                if final_sse_event is not None:
                    self._liveness.mark_activity()
                    yield final_sse_event
                sse_event_type = None
            finally:
                response.close()

            if self._state in ("stopping", "stopped", "failed"):
                return
            await asyncio.sleep(self._EVENT_RETRY_DELAY_SECONDS)

    async def _launch_process(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        host = config.ws_bind_host or "127.0.0.1"
        port = config.ws_port if config.ws_port > 0 else _find_free_port(host)
        self._base_url = f"http://{host}:{port}"
        command = project_managed_primary_backend_command(
            self.harness_id,
            spec,
            host=host,
            port=port,
        )
        env = inherit_child_env(os.environ, config.env_overrides)
        spawn_dir = resolve_spawn_log_dir(config.control_root, config.spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        _materialize_system_prompt(spawn_dir, config.system, env)
        self._stderr_log_path = spawn_dir / "stderr.log"
        self._stderr_handle = self._stderr_log_path.open("ab")
        self._stderr_read_offset = self._stderr_handle.tell()
        handle = await launch_managed_backend(
            ManagedBackendConfig(
                spawn_id=config.spawn_id,
                harness_id=self.harness_id,
                command=tuple(command),
                cwd=config.control_root,
                env=env,
                control_root=config.control_root,
            ),
            stderr=self._stderr_handle,
        )
        self._process = handle.process
        self._scope_handle = handle.scope_handle
        self._parent_death_link = handle.parent_death_link

    def _startup_timeout_budgets(self, configured_timeout: float | None) -> tuple[float, float]:
        if configured_timeout is None:
            return (self._READY_TIMEOUT_SECONDS, self._SESSION_STARTUP_TIMEOUT_SECONDS)

        total = max(configured_timeout, 0.2)
        readiness_timeout = max(total * (2.0 / 3.0), 0.1)
        session_timeout = max(total - readiness_timeout, 0.1)
        return (readiness_timeout, session_timeout)

    async def _wait_for_ready(self, *, timeout_seconds: float) -> None:
        deadline = time.monotonic() + max(timeout_seconds, 0.1)
        last_error: str | None = None

        while True:
            if self._process_exited():
                raise self._startup_exit_exception()

            for path in self._HEALTH_PATHS:
                try:
                    remaining = max(0.0, deadline - time.monotonic())
                    # Floor above zero: aiohttp treats ClientTimeout(total=0) as
                    # "no timeout", which would let a hung probe block forever.
                    probe_timeout = max(
                        min(remaining, self._PROBE_TIMEOUT_SECONDS), 0.05
                    )
                    status, body, _ = await self._get_json(path, timeout=probe_timeout)
                except TimeoutError as exc:
                    last_error = f"{path}: {exc or 'timeout'}"
                    if time.monotonic() >= deadline:
                        detail = f": {last_error}"
                        raise TimeoutError(
                            "OpenCode readiness endpoint did not become ready within "
                            f"{timeout_seconds:.1f}s ({', '.join(self._HEALTH_PATHS)}){detail}"
                        ) from exc
                    continue
                except Exception as exc:
                    if not _is_retryable_transport_error(exc):
                        raise
                    last_error = f"{path}: {exc}"
                    continue

                if status in self._SUCCESS_STATUSES:
                    self._last_health_ok = True
                    return
                last_error = f"{path}: status={status} body={_summarize_body(body)}"

            if time.monotonic() >= deadline:
                detail = f": {last_error}" if last_error else ""
                raise TimeoutError(
                    "OpenCode readiness endpoint did not become ready within "
                    f"{timeout_seconds:.1f}s ({', '.join(self._HEALTH_PATHS)}){detail}"
                )
            await asyncio.sleep(0.2)

    async def _create_session_with_retry(
        self,
        spec: ResolvedLaunchSpec,
        *,
        timeout_seconds: float,
    ) -> str:
        deadline = time.monotonic() + max(timeout_seconds, 0.1)
        last_error: Exception | None = None
        while True:
            if self._process_exited():
                raise self._startup_exit_exception()
            try:
                remaining = max(0.0, deadline - time.monotonic())
                session_id = await asyncio.wait_for(self._create_session(spec), timeout=remaining)
                self._last_health_ok = True
                return session_id
            except TimeoutError as exc:
                # POST /session is not idempotent: do not replay a create that may
                # have already taken effect server-side. Only transport-not-ready
                # (SessionNotReadyError) is safe to retry below.
                raise TimeoutError(
                    f"OpenCode session endpoint did not become ready within {timeout_seconds:.1f}s"
                ) from exc
            except SessionNotReadyError as exc:
                last_error = exc
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"OpenCode session endpoint did not become ready within {timeout_seconds:.1f}s"
                ) from last_error
            await asyncio.sleep(0.2)

    async def _create_session(self, spec: ResolvedLaunchSpec) -> str:
        """Create or resume an OpenCode session.

        For fresh sessions, POST /session to create a new one.
        For continues, verify the existing session exists via GET and return it
        directly — POST /session ignores sessionID in the payload and always
        creates a new empty session.
        """
        self._emit_startup_phase(StartupPhase.INITIALIZING_SESSION)

        # Validate unsupported modes before any network I/O.
        if spec.continue_fork:
            raise HarnessCapabilityMismatch(
                "OpenCode streaming cannot express continue_fork semantics over "
                "the current /session API."
            )

        continue_session_id = (spec.continue_session_id or "").strip()
        if continue_session_id:
            # Verify the existing session is already loaded by the server.
            # OpenCode serve loads sessions from disk on startup, so a GET should
            # find them. A 404/405 means the server may still be loading; we raise
            # a retryable error so _create_session_with_retry can poll until timeout.
            try:
                status, body, _ = await self._get_json(f"/session/{continue_session_id}")
            except Exception as exc:
                if _is_retryable_transport_error(exc):
                    raise SessionNotReadyError(
                        f"OpenCode session resume: session {continue_session_id} "
                        f"not reachable yet: {exc}"
                    ) from exc
                raise
            if status in self._SUCCESS_STATUSES:
                session_id = _extract_session_id(body)
                if session_id and session_id.strip() == continue_session_id:
                    logger.debug(
                        "OpenCode session resume: verified existing session %s",
                        continue_session_id,
                    )
                    return continue_session_id
                raise RuntimeError(
                    f"OpenCode session resume: GET returned mismatched id "
                    f"(expected={continue_session_id}, got={session_id})"
                )
            if status in self._PATH_RETRY_STATUSES:
                raise SessionNotReadyError(
                    f"OpenCode session resume: session {continue_session_id} not yet "
                    f"loaded (status={status})"
                )
            raise RuntimeError(f"OpenCode session resume: GET failed with status={status}")

        payload = project_opencode_spec_to_session_payload(
            spec,
            project_root=self._config.control_root if self._config is not None else None,
        )
        payload_variants: tuple[dict[str, object], ...] = (payload, {}) if payload else ({},)

        last_error: str | None = None
        for path in self._CREATE_SESSION_PATHS:
            for variant in payload_variants:
                try:
                    post_request = self._post_json(path, variant)
                    if variant:
                        status, body, _ = await asyncio.wait_for(
                            post_request,
                            timeout=self._SESSION_CREATE_PAYLOAD_TIMEOUT_SECONDS,
                        )
                    else:
                        status, body, _ = await post_request
                except TimeoutError:
                    if variant:
                        last_error = (
                            "OpenCode session create timed out with projected payload "
                            f"on {path}"
                        )
                        continue
                    raise
                except Exception as exc:
                    if _is_retryable_transport_error(exc):
                        raise SessionNotReadyError(
                            f"OpenCode session endpoint not reachable on {path}: {exc}"
                        ) from exc
                    raise
                if status in self._SUCCESS_STATUSES:
                    session_id = _extract_session_id(body)
                    if session_id is None:
                        raise RuntimeError(
                            f"OpenCode session creation response missing session id on {path}: "
                            f"{_summarize_body(body)}"
                        )
                    return session_id
                if status in self._PAYLOAD_RETRY_STATUSES:
                    last_error = (
                        f"OpenCode session create rejected payload on {path}: "
                        f"status={status} body={_summarize_body(body)}"
                    )
                    continue
                if status in self._PATH_RETRY_STATUSES:
                    trace_wire_recv(
                        self._tracer,
                        "http_probe",
                        "",
                        path=path,
                        status=status,
                        outcome="path_unavailable",
                    )
                    last_error = (
                        f"OpenCode session endpoint unavailable on {path}: "
                        f"status={status} body={_summarize_body(body)}"
                    )
                    raise SessionNotReadyError(last_error)
                raise RuntimeError(
                    f"OpenCode session creation failed on {path}: "
                    f"status={status} body={_summarize_body(body)}"
                )

        raise RuntimeError(last_error or "OpenCode session creation failed")

    async def _get_json(
        self,
        path: str,
        *,
        timeout: float | None = None,
    ) -> tuple[int, object | None, str]:
        """Perform a GET request and return (status, parsed_body, content_type).

        When *timeout* is set, the request is bounded by an aiohttp
        ``ClientTimeout`` so the underlying connection is torn down on expiry
        (a stuck connection is not returned to the keep-alive pool). The session
        default stays unbounded for long-lived streaming reads.
        """
        client = await self._ensure_http_client()
        trace_wire_send(self._tracer, "http_get", "", path=path)
        request_kwargs: dict[str, Any] = {}
        if timeout is not None:
            # aiohttp treats total=0 as "no timeout"; keep it strictly positive.
            request_kwargs["timeout"] = self._ensure_aiohttp().ClientTimeout(
                total=max(timeout, 0.001)
            )
        async with client.get(self._url(path), **request_kwargs) as response:
            status = int(response.status)
            content_type = str(response.headers.get("Content-Type", "")).lower()
            text_body = await response.text()
        parsed_body = _parse_response_body(text_body)
        trace_wire_recv(self._tracer, "http_response", text_body, path=path, status=status)
        return status, parsed_body, content_type

    async def _post_session_message(self, text: str, *, system: str | None = None) -> None:
        payload: dict[str, object] = {
            "parts": [{"type": "text", "text": text}],
        }
        if system and system.strip():
            payload["system"] = system
        await self._post_session_action(
            path_templates=self._MESSAGE_PATH_TEMPLATES,
            payload_variants=(payload,),
            accepted_statuses=self._SUCCESS_STATUSES,
        )

    async def _post_session_action(
        self,
        *,
        path_templates: tuple[str, ...],
        payload_variants: tuple[dict[str, object], ...],
        accepted_statuses: frozenset[int],
    ) -> None:
        session_id = self._require_session_id()
        last_error: str | None = None

        for template in path_templates:
            path = template.format(session_id=session_id)
            for payload in payload_variants:
                status, body, _content_type = await self._post_json(
                    path,
                    payload,
                    skip_body_on_statuses=accepted_statuses,
                    tolerate_incomplete_body=True,
                )
                if status in accepted_statuses:
                    return
                if status in self._PAYLOAD_RETRY_STATUSES:
                    last_error = (
                        f"OpenCode session action rejected payload on {path}: "
                        f"status={status} body={_summarize_body(body)}"
                    )
                    continue
                if status in self._PATH_RETRY_STATUSES:
                    last_error = (
                        f"OpenCode session endpoint unavailable on {path}: "
                        f"status={status} body={_summarize_body(body)}"
                    )
                    break
                raise RuntimeError(
                    f"OpenCode session action failed on {path}: "
                    f"status={status} body={_summarize_body(body)}"
                )

        raise RuntimeError(last_error or "OpenCode session action failed")

    async def _post_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        skip_body_on_statuses: frozenset[int] | None = None,
        tolerate_incomplete_body: bool = False,
    ) -> tuple[int, object | None, str]:
        client = await self._ensure_http_client()
        request_key = f"post:{path}"
        trace_wire_send(
            self._tracer,
            "http_post",
            json.dumps(dict(payload)),
            path=path,
        )
        self._liveness.signal_request_in_flight(request_key)
        try:
            async with client.post(self._url(path), json=dict(payload)) as response:
                status = int(response.status)
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if skip_body_on_statuses is not None and status in skip_body_on_statuses:
                    response.release()
                    return status, None, content_type
                try:
                    text_body = await response.text()
                except Exception as exc:
                    aiohttp = self._ensure_aiohttp()
                    client_payload_error = getattr(aiohttp, "ClientPayloadError", None)
                    if (
                        tolerate_incomplete_body
                        and client_payload_error is not None
                        and isinstance(exc, client_payload_error)
                    ):
                        logger.warning(
                            "Ignoring incomplete OpenCode response body on %s (status=%s)",
                            path,
                            status,
                        )
                        return status, None, content_type
                    raise
            parsed_body = _parse_response_body(text_body)
            trace_wire_recv(
                self._tracer,
                "http_response",
                text_body,
                path=path,
                status=status,
            )
            return status, parsed_body, content_type
        finally:
            self._liveness.signal_request_resolved(request_key)

    async def _open_event_stream(self) -> Any:
        client = await self._ensure_http_client()
        session_id = self._require_session_id()
        paths: list[str] = []
        if self._event_path is not None:
            paths.append(self._event_path)
        for template in self._EVENT_PATHS:
            path = template.format(session_id=session_id)
            if path not in paths:
                paths.append(path)

        last_error: str | None = None
        for path in paths:
            response = await client.get(
                self._url(path),
                headers=self._EVENT_ACCEPT_HEADER,
                timeout=None,
            )
            status = int(response.status)
            if status in self._SUCCESS_STATUSES:
                self._event_path = path
                trace_wire_recv(
                    self._tracer,
                    "sse_connect",
                    "",
                    path=path,
                    status=status,
                )
                return response

            body = await response.text()
            response.release()

            if status in self._PATH_RETRY_STATUSES:
                trace_wire_recv(
                    self._tracer,
                    "http_probe",
                    "",
                    path=path,
                    status=status,
                    outcome="path_unavailable",
                )
                last_error = (
                    f"OpenCode event endpoint unavailable on {path}: "
                    f"status={status} body={_summarize_body(body)}"
                )
                continue
            raise RuntimeError(
                f"OpenCode event stream failed on {path}: "
                f"status={status} body={_summarize_body(body)}"
            )

        raise RuntimeError(last_error or "OpenCode event stream endpoint unavailable")

    def _consume_stream_line(
        self,
        line: str,
        *,
        sse_event_type: str | None,
        sse_data_lines: list[str],
    ) -> tuple[HarnessEvent | None, str | None]:
        if not line:
            event = self._flush_sse_event(
                sse_event_type=sse_event_type,
                sse_data_lines=sse_data_lines,
            )
            return event, None

        if line.startswith(":"):
            return None, sse_event_type

        if line.startswith("event:"):
            event_name = line.split(":", maxsplit=1)[1].strip()
            return None, event_name or sse_event_type

        if line.startswith("data:"):
            sse_data_lines.append(line.split(":", maxsplit=1)[1].lstrip())
            return None, sse_event_type

        event = self._event_from_json_line(
            line,
            raw_text=line,
            event_type_hint=sse_event_type,
        )
        return event, sse_event_type

    def _flush_sse_event(
        self,
        *,
        sse_event_type: str | None,
        sse_data_lines: list[str],
    ) -> HarnessEvent | None:
        if not sse_data_lines:
            return None
        payload_text = "\n".join(sse_data_lines)
        sse_data_lines.clear()
        return self._event_from_json_line(
            payload_text,
            raw_text=payload_text,
            event_type_hint=sse_event_type,
        )

    def _event_from_json_line(
        self,
        json_text: str,
        *,
        raw_text: str,
        event_type_hint: str | None = None,
    ) -> HarnessEvent | None:
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed OpenCode stream line: %s", raw_text)
            trace_parse_error(self._tracer, "opencode", raw_text, error="malformed_json")
            return None

        payload: dict[str, object]
        if isinstance(parsed, dict):
            payload = cast("dict[str, object]", parsed)
        else:
            payload = {"value": cast("object", parsed)}

        nested_payload = payload.get("payload")
        if isinstance(nested_payload, dict):
            payload = cast("dict[str, object]", nested_payload)

        raw_event_type = payload.get("type", event_type_hint or "unknown")
        event_type = raw_event_type if isinstance(raw_event_type, str) else "unknown"
        event = HarnessEvent(
            event_type=event_type,
            payload=payload,
            harness_id=HarnessId.OPENCODE.value,
            raw_text=raw_text,
        )
        if clears_signal(event, primary_event_scope=self.primary_event_scope):
            self._signal_in_flight = False
            self._liveness.signal_request_resolved("cancel")
        return event

    async def _ensure_http_client(self) -> Any:
        if self._client is not None:
            return self._client
        aiohttp = self._ensure_aiohttp()
        timeout = aiohttp.ClientTimeout(total=None)
        self._client = aiohttp.ClientSession(timeout=timeout)
        return self._client

    def _ensure_aiohttp(self) -> Any:
        if self._aiohttp_module is not None:
            return self._aiohttp_module
        self._aiohttp_module = importlib.import_module("aiohttp")
        return self._aiohttp_module

    async def _cleanup_runtime(self) -> None:
        self._liveness.reset()
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.warning("Failed to close OpenCode HTTP client", exc_info=True)

        scope_handle = self._scope_handle
        self._scope_handle = None
        process = self._process
        self._process = None
        if scope_handle is not None and process is not None and process.returncode is None:
            await scope_handle.terminate(
                grace_seconds=self._STOP_GRACE_SECONDS,
                reason="stop_called",
            )
        elif process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self._STOP_GRACE_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()

        self._parent_death_link = None
        self._base_url = None
        self._session_id = None
        self._event_path = None
        self._last_health_ok = False
        self._cancel_requested = False
        self._signal_in_flight = False
        self._liveness.signal_request_resolved("cancel")
        self._close_log_handles()

    def _url(self, path: str) -> str:
        if self._base_url is None:
            raise RuntimeError("OpenCode base URL is not initialized")
        return f"{self._base_url}{path}"

    def _require_session_id(self) -> str:
        if self._session_id is None:
            raise ConnectionNotReady("OpenCode session has not been created yet")
        return self._session_id

    def _require_connected(self) -> None:
        if self._state != "connected":
            raise ConnectionNotReady(
                f"OpenCode connection is not ready (current state: {self._state})"
            )

    def _process_exited(self) -> bool:
        process = self._process
        if process is None:
            return False
        return process.returncode is not None

    def _set_failed(self) -> None:
        if self._state == "failed":
            return
        if self._state == "stopped":
            return
        self._transition("failed")

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
                "OpenCode backend failed to bind HTTP port "
                f"(exit={exit_code}): {stderr_excerpt or '<no stderr>'}"
            )
        message = _format_opencode_startup_failure_message(
            exit_code=exit_code,
            stderr_excerpt=stderr_excerpt,
            env=self._startup_child_env(),
        )
        return RuntimeError(message)

    def _startup_child_env(self) -> dict[str, str]:
        config = self._config
        if config is None:
            return dict(os.environ)
        return inherit_child_env(os.environ, config.env_overrides)

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
        return (
            self._state not in {"starting", "connected"}
            or not process_running
            or not self._last_health_ok
        )

    def _transition(self, next_state: ConnectionState) -> None:
        if next_state == self._state:
            return
        allowed = self._STATE_TRANSITIONS[self._state]
        if next_state not in allowed:
            raise RuntimeError(f"Invalid OpenCode state transition: {self._state} -> {next_state}")
        trace_state_change(self._tracer, "opencode", self._state, next_state)
        self._state = next_state

    def _emit_startup_phase(self, phase: StartupPhase) -> None:
        emitter = self._startup_emitter
        if emitter is not None:
            emitter.emit(phase)


def _parse_response_body(text_body: str) -> object | None:
    stripped = text_body.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _extract_session_id(body: object | None) -> str | None:
    if body is None:
        return None
    if isinstance(body, str):
        normalized = body.strip()
        return normalized or None
    if isinstance(body, Mapping):
        mapping_body = cast("Mapping[str, object]", body)
        return _extract_session_id_from_mapping(mapping_body)
    return None


def _extract_session_id_from_mapping(data: Mapping[str, object]) -> str | None:
    direct_keys = ("session_id", "sessionId", "sessionID", "id")
    for key in direct_keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    nested = data.get("session")
    if isinstance(nested, Mapping):
        nested_mapping = cast("Mapping[str, object]", nested)
        for key in direct_keys:
            value = nested_mapping.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _summarize_body(body: object | None) -> str:
    if body is None:
        return "<empty>"
    if isinstance(body, str):
        trimmed = body.strip()
        if not trimmed:
            return "<empty>"
        return trimmed[:200]
    try:
        serialized = json.dumps(body, ensure_ascii=True)
    except TypeError:
        return repr(body)[:200]
    return serialized[:200]


def _is_retryable_transport_error(exc: Exception) -> bool:
    if isinstance(exc, OSError | TimeoutError):
        return True
    try:
        aiohttp = importlib.import_module("aiohttp")
    except ImportError:
        return False
    client_error = getattr(aiohttp, "ClientError", None)
    return client_error is not None and isinstance(exc, client_error)


def _materialize_system_prompt(
    spawn_dir: Path,
    system: str | None,
    env: dict[str, str],
) -> None:
    """Write system prompt to a temp file and inject it as an OpenCode instruction.

    A unique temp file is created under the system temp directory so the path
    is opaque to the model (OpenCode prefixes each instruction with
    ``Instructions from: <path>`` which the model can see).  The file only
    needs to live as long as the ``opencode serve`` process.
    """
    _ = spawn_dir  # reserved for future use (cleanup tracking)
    text = (system or "").strip()
    if not text:
        return
    fd, tmp_path = tempfile.mkstemp(prefix="meridian-sysprompt-", suffix=".md")
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    absolute_path = os.path.abspath(tmp_path)

    existing_raw = env.get(OPENCODE_CONFIG_CONTENT_ENV, "").strip()
    existing: dict[str, object] = {}
    if existing_raw:
        try:
            parsed = json.loads(existing_raw)
            if isinstance(parsed, dict):
                existing = cast("dict[str, object]", parsed)
        except json.JSONDecodeError:
            logger.warning(
                "Failed to parse existing %s; overwriting with instruction config",
                OPENCODE_CONFIG_CONTENT_ENV,
            )

    instructions: list[str] = []
    prev: object = existing.get("instructions")
    if isinstance(prev, list):
        for entry in cast("list[object]", prev):
            if isinstance(entry, str):
                instructions.append(entry)
    instructions.append(absolute_path)
    existing["instructions"] = instructions

    env[OPENCODE_CONFIG_CONTENT_ENV] = json.dumps(existing, separators=(",", ":"))


def _looks_like_address_in_use(stderr_text: str) -> bool:
    normalized = stderr_text.lower()
    return any(marker in normalized for marker in _ADDRESS_IN_USE_MARKERS)


def _format_opencode_startup_failure_message(
    *,
    exit_code: int | None,
    stderr_excerpt: str,
    env: Mapping[str, str],
) -> str:
    lines = [f"OpenCode backend failed to start (exit={exit_code})."]
    if stderr_excerpt:
        lines.append(stderr_excerpt)
    hint = _opencode_startup_failure_hint(stderr_excerpt, env)
    if hint is not None:
        lines.append("")
        lines.append(f"Hint: {hint}")
    return "\n".join(lines)


def _opencode_startup_failure_hint(stderr_text: str, env: Mapping[str, str]) -> str | None:
    normalized = stderr_text.lower()
    if not normalized:
        return None

    data_dir_markers = (
        "eacces",
        "eperm",
        "permission denied",
        "enotdir",
        "read-only file system",
        "erofs",
    )
    if not any(marker in normalized for marker in data_dir_markers):
        return None
    if "mkdir" not in normalized and "opencode" not in normalized:
        return None

    xdg_data_home = env.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return (
            f"OpenCode cannot write its data directory under XDG_DATA_HOME ({xdg_data_home}). "
            "Ensure that path exists and is writable, or unset XDG_DATA_HOME to use the default "
            "(~/.local/share)."
        )
    return (
        "OpenCode cannot create its data directory. Check permissions for ~/.local/share/opencode, "
        "or set XDG_DATA_HOME to a writable directory."
    )
