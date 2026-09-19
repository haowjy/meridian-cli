"""HTTP-backed bidirectional OpenCode harness connection."""

from __future__ import annotations

import asyncio
import errno
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
    ObserverEndpoint,
    RawHarnessEvent,
    StopProgressCallback,
    StopResult,
    reap_on_ownership_transfer_failure,
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
    opencode_model_parts,
    project_opencode_model_config,
)
from meridian.lib.harness.projections.project_opencode_streaming import (
    project_opencode_spec_to_session_payload as _project_opencode_spec_to_session_payload,
)
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.harness.semantics import (
    EventSemantics,
    PrimaryEventScope,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.workspace_projection import OPENCODE_CONFIG_CONTENT_ENV
from meridian.lib.observability.trace_helpers import (
    trace_parse_error,
    trace_state_change,
    trace_wire_recv,
    trace_wire_send,
)
from meridian.lib.platform.detached_process import ParentDeathLink, release_parent_death_link
from meridian.lib.platform.process_scope import (
    ProcessScopeSnapshot,
    ScopedProcessHandle,
    is_pgid_reachable,
)
from meridian.lib.state.paths import (
    resolve_project_runtime_root_for_write,
    resolve_spawn_log_dir,
)

logger = logging.getLogger(__name__)
_STARTUP_STDERR_MAX_BYTES = 16 * 1024
_ADDRESS_IN_USE_MARKERS = ("address already in use", "address in use", "eaddrinuse")


class SessionNotReadyError(RuntimeError):
    """Retryable OpenCode session readiness failure."""


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


class OpenCodeV1Connection(HarnessConnection[ResolvedLaunchSpec]):
    """Bidirectional OpenCode 1.x connection over the legacy JSON HTTP API."""

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
                StartupPhase.LAUNCHING_SUBPROCESS,
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
    _CREATE_SESSION_PATH: ClassVar[str] = "/session"
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
    # Re-polls allowed on the liveness-timeout path before a stall is declared
    # terminal. One is enough to cover the prompt-before-subscribe gap; the cap
    # keeps recovery bounded if the timeout path is ever revisited.
    _STALL_RECONCILE_LIMIT: ClassVar[int] = 1
    _STARTUP_TIMEOUT_SECONDS: ClassVar[float] = 90.0
    _READY_TIMEOUT_SECONDS: ClassVar[float] = 60.0
    _SESSION_STARTUP_TIMEOUT_SECONDS: ClassVar[float] = (
        _STARTUP_TIMEOUT_SECONDS - _READY_TIMEOUT_SECONDS
    )
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
        self._model_agent_override: str | None = None
        self._instruction_path: Path | None = None
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
        self._stop_lock = asyncio.Lock()

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
        session_id = (self._session_id or "").strip()
        if not session_id:
            return None
        return PrimaryEventScope(harness_id=HarnessId.OPENCODE, scope_id=session_id)

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

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        await self._start(config, spec, observer=False)

    async def start_observer(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        """Start connection in primary observer mode."""
        await self._start(config, spec, observer=True)

    async def _start(
        self, config: ConnectionConfig, spec: ResolvedLaunchSpec, *, observer: bool
    ) -> None:
        # Keep ownership publication, connected state, and failure cleanup inside
        # one lifecycle gate. A concurrent stop cannot acknowledge an absent child
        # while process creation is still in flight.
        await self._stop_lock.acquire()
        if self._state not in {"created", "stopped", "failed"}:
            self._stop_lock.release()
            raise RuntimeError(f"Cannot start OpenCode connection from state '{self._state}'")
        self._primary_observer_mode = observer
        try:
            await self._start_unlocked(config, spec)
        except BaseException:
            self._set_failed()
            # Cleanup owns the gate even if the bounded foreground wait expires.
            await reap_on_ownership_transfer_failure(self._cleanup_start_failure)
            raise
        else:
            self._stop_lock.release()

    async def _cleanup_start_failure(self) -> None:
        try:
            await self._cleanup_runtime()
        finally:
            self._stop_lock.release()

    async def _start_unlocked(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        if (
            self._process is not None
            or self._scope_handle is not None
            or self._instruction_path is not None
        ):
            # A failed cleanup must not lose its remaining ownership on retry.
            await self._cleanup_runtime()
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

        readiness_timeout, session_timeout = self._startup_timeout_budgets(config.timeout_seconds)

        self._model_agent_override = None
        deadline = asyncio.get_running_loop().time() + readiness_timeout + session_timeout
        async with asyncio.timeout_at(deadline):
            self._emit_startup_phase(StartupPhase.LAUNCHING_SUBPROCESS)
            await self._launch_process(config, spec)
            self._emit_startup_phase(StartupPhase.WAITING_FOR_CONNECTION)
            await self._wait_for_ready(timeout_seconds=readiness_timeout)
            if spec.model and not spec.continue_session_id:
                conflict = await self._inspect_selected_model(spec.model)
                if conflict is not None:
                    await self._cleanup_runtime(replacement_deadline=deadline)
                    self._model_agent_override = conflict
                    await self._launch_process(config, spec)
                    await self._wait_for_ready(timeout_seconds=readiness_timeout)
                    if await self._inspect_selected_model(spec.model, expected_agent=conflict):
                        raise HarnessCapabilityMismatch(
                            "OpenCode native agent still overrides the selected model"
                        )
            self._session_id = await self._create_session_with_retry(
                spec,
                timeout_seconds=min(
                    session_timeout, max(0, deadline - asyncio.get_running_loop().time())
                ),
            )
            if config.session_id_observer is not None:
                config.session_id_observer(self._session_id)
            if not self._primary_observer_mode:
                await self._post_session_message(
                    config.prompt,
                    system=config.system,
                    fresh=not bool(spec.continue_session_id),
                    model=spec.model,
                )
        self._transition("connected")
        self._emit_startup_phase(StartupPhase.HARNESS_READY)
        self._last_health_ok = True

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

    async def _reconcile_initial_terminal(self) -> RawHarnessEvent | None:
        """Return a terminal event missed by the prompt-before-subscribe window.

        ``start()`` posts the initial prompt before the drain loop attaches to
        ``events()``. A turn that fails (or succeeds) in that gap can lose its
        terminal frame. The 1.x server replays pre-subscription frames on
        ``/global/event``, so this hook is a no-op here; V2 overrides it because
        its ``/api/event`` stream is live-only.
        """

        return None

    async def _reconcile_on_stall(self) -> RawHarnessEvent | None:
        """Recover a terminal the live stream may have missed on a stall.

        ``events()`` subscribes after the initial prompt in ``start()``. A turn
        that finishes in that gap can lose its terminal frame. The 1.x server
        replays pre-subscription frames, so the stream itself recovers it and
        this hook is a no-op; V2 overrides it because ``/api/event`` is
        live-only. Called at most ``_STALL_RECONCILE_LIMIT`` times per drain.
        """

        return None

    async def events(self) -> AsyncIterator[RawHarnessEvent]:
        if self._state not in ("connected", "stopping"):
            return
        if self._session_id is None:
            return

        reconciled = await self._reconcile_initial_terminal()
        if reconciled is not None:
            self._liveness.mark_activity()
            yield reconciled
            return

        stall_reconciles_remaining = self._STALL_RECONCILE_LIMIT

        async def _reconcile_before_stall() -> RawHarnessEvent | None:
            """Re-poll the durable outcome before declaring the stream stalled."""

            nonlocal stall_reconciles_remaining
            if stall_reconciles_remaining <= 0:
                return None
            stall_reconciles_remaining -= 1
            return await self._reconcile_on_stall()

        sse_event_type: str | None = None
        sse_data_lines: list[str] = []

        while self._state in ("connected", "stopping"):
            if self._process_exited():
                event = self._process_exit_event()
                if event is not None:
                    yield event
                return
            if self._liveness.evaluate() in (
                LivenessDecision.BACKEND_DEAD,
                LivenessDecision.STREAM_STALLED,
            ):
                reconciled = await _reconcile_before_stall()
                if reconciled is not None:
                    self._liveness.mark_activity()
                    yield reconciled
                    return
                logger.warning(
                    "OpenCode event stream liveness timeout after %.1fs without events",
                    self._LIVENESS_TIMEOUT_SECONDS,
                )
                self._set_failed()
                return

            self._liveness.mark_activity_if_idle()
            try:
                response = await self._liveness.wait_for_activity(self._open_event_stream())
            except EventStreamLivenessTimeout:
                if self._process_exited():
                    event = self._process_exit_event()
                    if event is not None:
                        yield event
                    return
                reconciled = await _reconcile_before_stall()
                if reconciled is not None:
                    self._liveness.mark_activity()
                    yield reconciled
                    return
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
                    event = self._process_exit_event()
                    if event is not None:
                        yield event
                    return
                logger.warning("OpenCode event stream dropped; reconnecting: %s", exc)
                await asyncio.sleep(self._EVENT_RETRY_DELAY_SECONDS)
                continue

            buffer = ""
            try:
                while self._state not in ("stopping", "stopped", "failed"):
                    try:
                        chunk = await self._liveness.wait_for_activity(response.content.read(4096))
                    except EventStreamLivenessTimeout:
                        if self._process_exited():
                            event = self._process_exit_event()
                            if event is not None:
                                yield event
                            return
                        reconciled = await _reconcile_before_stall()
                        if reconciled is not None:
                            self._liveness.mark_activity()
                            yield reconciled
                            return
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
        env = dict(config.child_env)
        if spec.model and not spec.continue_session_id:
            env[OPENCODE_CONFIG_CONTENT_ENV] = project_opencode_model_config(
                env.get(OPENCODE_CONFIG_CONTENT_ENV), spec.model, self._model_agent_override
            )
        runtime_root = config.runtime_root or resolve_project_runtime_root_for_write(
            config.control_root
        )
        spawn_dir = resolve_spawn_log_dir(
            config.control_root, config.spawn_id, runtime_root=runtime_root
        )
        self._instruction_path = _materialize_system_prompt(config.system, env)
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
                    probe_timeout = max(min(remaining, self._PROBE_TIMEOUT_SECONDS), 0.05)
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
            if spec.model:
                # V1 resumes by GET and cannot change the committed model. Fail
                # loudly instead of silently retaining the native model.
                raise HarnessCapabilityMismatch(
                    "OpenCode 1.x cannot switch the model when resuming a session "
                    f"(requested model '{spec.model}'). Upgrade to OpenCode 2 or "
                    "omit the explicit --model."
                )
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
        # POST /session is not idempotent. Do not retry a rejected model with
        # an empty payload, or replay a request whose response was lost.
        path = self._CREATE_SESSION_PATH
        status, body, _ = await self._post_json(path, payload)
        if status in self._SUCCESS_STATUSES:
            session_id = _extract_session_id(body)
            if session_id is None:
                raise RuntimeError(
                    "OpenCode session creation response missing session id: "
                    f"{_summarize_body(body)}"
                )
            return session_id
        if status in self._PATH_RETRY_STATUSES:
            raise SessionNotReadyError(f"OpenCode session endpoint unavailable: status={status}")
        raise RuntimeError(
            f"OpenCode session create rejected payload: status={status} "
            f"body={_summarize_body(body)}"
        )

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

    async def _native_json(self, path: str) -> object:
        status, body, _ = await self._get_json(path)
        if status not in self._SUCCESS_STATUSES:
            raise HarnessCapabilityMismatch(f"OpenCode cannot inspect {path}: status={status}")
        return body

    async def _inspect_selected_model(
        self, model: str, *, expected_agent: str | None = None
    ) -> str | None:
        provider, model_id = opencode_model_parts(model)
        providers = await self._native_json("/config/providers")
        available = (
            cast("dict[str, object]", providers).get("providers")
            if isinstance(providers, dict)
            else None
        )
        if not isinstance(available, list) or not any(
            row.get("id") == provider
            and isinstance(row.get("models"), dict)
            and model_id in cast("dict[str, object]", row["models"])
            for row in (
                cast("dict[str, object]", item)
                for item in cast("list[object]", available)
                if isinstance(item, dict)
            )
        ):
            raise HarnessCapabilityMismatch(f"OpenCode selected model is unavailable: {model}")
        config = await self._native_json("/config")
        if not isinstance(config, dict):
            raise HarnessCapabilityMismatch("OpenCode returned invalid configuration")
        config = cast("dict[str, object]", config)
        if config.get("model") != model:
            raise HarnessCapabilityMismatch(
                "OpenCode effective configuration overrides the selected model"
            )
        agents = await self._native_json("/agent")
        if not isinstance(agents, list):
            raise HarnessCapabilityMismatch("OpenCode returned invalid primary agents")
        agent = next(
            (
                row
                for row in (
                    cast("dict[str, object]", item)
                    for item in cast("list[object]", agents)
                    if isinstance(item, dict)
                )
                if row.get("mode") != "subagent" and not row.get("hidden")
            ),
            None,
        )
        if agent is None or not isinstance(agent.get("name"), str):
            raise HarnessCapabilityMismatch("OpenCode has no visible native primary agent")
        name = cast("str", agent["name"])
        if expected_agent is not None and name != expected_agent:
            raise HarnessCapabilityMismatch(
                "OpenCode native primary agent changed during configuration"
            )
        configured = config.get("default_agent")
        if configured is not None and configured != name:
            raise HarnessCapabilityMismatch(
                "OpenCode default agent does not match its visible primary"
            )
        native_model = agent.get("model")
        if native_model is None:
            return None
        if not isinstance(native_model, dict):
            raise HarnessCapabilityMismatch("OpenCode native agent has invalid model configuration")
        native_model = cast("dict[str, object]", native_model)
        return (
            None
            if native_model.get("providerID") == provider
            and native_model.get("modelID") == model_id
            else name
        )

    async def _post_session_message(
        self, text: str, *, system: str | None = None, fresh: bool = False, model: str | None = None
    ) -> None:
        payload: dict[str, object] = {"parts": [{"type": "text", "text": text}]}
        if fresh:
            if model is not None:
                provider, model_id = opencode_model_parts(model)
                payload["model"] = {"providerID": provider, "modelID": model_id}
        else:
            native = await self._native_json(f"/session/{self._require_session_id()}")
            native = cast("dict[str, object]", native) if isinstance(native, dict) else {}
            choice = native.get("model")
            agent = native.get("agent")
            if not isinstance(choice, dict) or not isinstance(agent, str) or not agent:
                messages = await self._native_json(f"/session/{self._require_session_id()}/message")
                if isinstance(messages, list):
                    for item in reversed(cast("list[object]", messages)):
                        info = (
                            cast("dict[str, object]", item).get("info")
                            if isinstance(item, dict)
                            else None
                        )
                        if not isinstance(info, dict):
                            continue
                        info = cast("dict[str, object]", info)
                        if info.get("role") != "user":
                            continue
                        latest = info.get("model")
                        if isinstance(latest, dict):
                            latest = cast("dict[str, object]", latest)
                            choice = {
                                "id": latest.get("modelID"),
                                "providerID": latest.get("providerID"),
                                "variant": latest.get("variant", "default"),
                            }
                            agent = info.get("agent")
                        break
            if not isinstance(choice, dict) or not isinstance(agent, str) or not agent:
                raise HarnessCapabilityMismatch(
                    "OpenCode has no observable committed agent/model for this injection"
                )
            choice = cast("dict[str, object]", choice)
            provider, model_id = choice.get("providerID"), choice.get("id")
            if (
                not isinstance(provider, str)
                or not isinstance(model_id, str)
                or not provider
                or not model_id
            ):
                raise HarnessCapabilityMismatch("OpenCode returned an invalid committed model")
            payload["agent"] = agent
            payload["model"] = {"providerID": provider, "modelID": model_id}
            variant = choice.get("variant")
            if isinstance(variant, str):
                payload["variant"] = variant
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
    ) -> tuple[RawHarnessEvent | None, str | None]:
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
    ) -> RawHarnessEvent | None:
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
    ) -> RawHarnessEvent | None:
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
        event = RawHarnessEvent(
            event_type=event_type,
            payload=payload,
            harness_id=HarnessId.OPENCODE.value,
            raw_text=raw_text,
        )
        return event

    def observe_event_semantics(self, semantics: EventSemantics) -> None:
        if semantics.clears_signal:
            self._signal_in_flight = False
            self._liveness.signal_request_resolved("cancel")

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

    async def _cleanup_runtime(self, *, replacement_deadline: float | None = None) -> None:
        self._liveness.reset()
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.warning("Failed to close OpenCode HTTP client", exc_info=True)

        scope_handle = self._scope_handle
        process = self._process
        grace = self._STOP_GRACE_SECONDS
        if replacement_deadline is not None:
            if scope_handle is None or process is None:
                raise RuntimeError("OpenCode replacement requires an owned process scope")
            # POSIX scope termination can synchronously spend one extra second
            # awaiting SIGKILL. Reserve that within the shared startup budget.
            remaining = replacement_deadline - asyncio.get_running_loop().time()
            if remaining <= 1:
                raise TimeoutError("OpenCode startup budget exhausted before replacement")
            grace = min(grace, remaining - 1)
        if scope_handle is not None and process is not None:
            result = await scope_handle.terminate(
                grace_seconds=grace,
                reason="stop_called",
            )
            if replacement_deadline is not None:
                remaining = replacement_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("OpenCode startup budget exhausted during replacement")
                await asyncio.wait_for(process.wait(), timeout=remaining)
                snapshot = scope_handle.snapshot
                if (
                    result.skip_reason
                    or result.degraded_fallback
                    or (snapshot.pgid is not None and is_pgid_reachable(snapshot.pgid))
                ):
                    raise RuntimeError(
                        "OpenCode backend cleanup is uncertain; refusing replacement"
                    )
        elif process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self._STOP_GRACE_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()

        self._scope_handle = None
        self._process = None
        await asyncio.to_thread(release_parent_death_link, self._parent_death_link)
        self._parent_death_link = None
        self._base_url = None
        self._session_id = None
        self._event_path = None
        self._last_health_ok = False
        self._cancel_requested = False
        self._signal_in_flight = False
        self._liveness.signal_request_resolved("cancel")
        self._close_log_handles()
        if self._instruction_path is not None:
            self._instruction_path.unlink(missing_ok=True)
            self._instruction_path = None

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

    def _process_exit_event(self) -> RawHarnessEvent | None:
        if self._state in {"stopping", "stopped"}:
            return None
        process = self._process
        return_code = process.returncode if process is not None else None
        if return_code is None:
            raise RuntimeError("OpenCode process-exit event requested before process exit")
        detail = f"OpenCode subprocess exited with code {return_code}."
        stderr_excerpt = self._read_startup_stderr_excerpt()
        if stderr_excerpt:
            detail = f"{detail}\n\nOpenCode subprocess stderr:\n{stderr_excerpt}"
        self._set_failed()
        return RawHarnessEvent(
            event_type="meridian/error/connectionClosed",
            payload={"type": "meridian/error/connectionClosed", "message": detail},
            harness_id=self.harness_id.value,
        )

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
        if _looks_like_address_in_use(stderr_excerpt) or self._startup_port_is_claimed():
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

    def _startup_port_is_claimed(self) -> bool:
        """Detect OpenCode's generic ServeError when another process won the port."""

        if self._base_url is None:
            return False
        endpoint = urlparse(self._base_url)
        if endpoint.hostname is None or endpoint.port is None:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((endpoint.hostname, endpoint.port))
            except OSError as exc:
                return exc.errno == errno.EADDRINUSE
        return False

    def _startup_child_env(self) -> dict[str, str]:
        config = self._config
        if config is None:
            raise RuntimeError("OpenCode startup diagnostics require a bound child environment")
        return dict(config.child_env)

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


def _materialize_system_prompt(system: str | None, env: dict[str, str]) -> Path | None:
    """Create one owned, private instruction file for this backend attempt."""
    raw = env.get(OPENCODE_CONFIG_CONTENT_ENV, "").strip()
    parsed: object = json.loads(raw) if raw else {}
    if not isinstance(parsed, dict):
        raise HarnessCapabilityMismatch("OpenCode config content must be a JSON object")
    config = cast("dict[str, object]", parsed)
    text = (system or "").strip()
    if not text:
        return None
    previous = config.get("instructions", [])
    if not isinstance(previous, list) or not all(
        isinstance(item, str) for item in cast("list[object]", previous)
    ):
        raise HarnessCapabilityMismatch("OpenCode instructions must be a list of paths")
    fd, name = tempfile.mkstemp(prefix="meridian-sysprompt-", suffix=".md")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        config["instructions"] = [*cast("list[str]", previous), str(path)]
        env[OPENCODE_CONFIG_CONTENT_ENV] = json.dumps(config, separators=(",", ":"))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


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


# Compatibility alias: existing tests and callers import the 1.x transport as
# ``OpenCodeConnection``. The registered connection class is the version
# dispatcher in ``opencode_connection.py``.
OpenCodeConnection = OpenCodeV1Connection
