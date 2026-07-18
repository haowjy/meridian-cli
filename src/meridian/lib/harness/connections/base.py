"""Core data contracts for bidirectional harness connections."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Generic, Literal, Protocol

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.launch.launch_types import SpecT

if TYPE_CHECKING:
    from meridian.lib.harness.connections.resident_backend import ResidentBackendControl
    from meridian.lib.harness.semantics import PrimaryEventScope
    from meridian.lib.observability.debug_tracer import DebugTracer
    from meridian.lib.platform.process_scope import ProcessScopeSnapshot

# Uniform transport message cap across harness adapters.
MAX_HARNESS_MESSAGE_BYTES: Final[int] = 10 * 1024 * 1024

# Uniform initial-prompt cap across adapters; fail loudly if the prompt is too large.
MAX_INITIAL_PROMPT_BYTES: Final[int] = 10 * 1024 * 1024

# Foreground ownership-transfer cleanup bound; residue is owned by durable scopes.
OWNERSHIP_TRANSFER_REAP_TIMEOUT_SECONDS: Final[float] = 30.0

logger = logging.getLogger(__name__)


def _empty_startup_phases() -> frozenset[str]:
    return frozenset()


class PromptTooLargeError(RuntimeError):
    """Raised when the initial prompt exceeds the harness adapter byte limit."""

    def __init__(self, actual_bytes: int, max_bytes: int, harness: str) -> None:
        super().__init__(
            f"{harness}: initial prompt is {actual_bytes} bytes, exceeds limit of {max_bytes} bytes"
        )
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        self.harness = harness


@dataclass(frozen=True)
class ConnectionCapabilities:
    """Feature flags describing one bidirectional connection implementation."""

    mid_turn_injection: Literal["queue", "interrupt_restart", "http_post"]
    supports_steer: bool
    supports_cancel: bool
    runtime_model_switch: bool
    structured_reasoning: bool
    supports_primary_observer: bool = False
    supports_runtime_hitl: bool = False
    supported_startup_phases: frozenset[str] = field(default_factory=_empty_startup_phases)
    """Startup phases this adapter can observe. Empty = unknown/untyped."""


@dataclass(frozen=True)
class ObserverEndpoint:
    """Attach endpoint exposed by a managed primary backend."""

    transport: Literal["ws", "http"]
    url: str
    host: str | None = None
    port: int | None = None


ConnectionState = Literal["created", "starting", "connected", "stopping", "stopped", "failed"]
PiSessionRole = Literal["primary", "spawned"]


class ConnectionNotReady(RuntimeError):
    """Raised when send operations are attempted before connection readiness."""


@dataclass(frozen=True)
class StopResult:
    """Normalized stop outcome metadata across harness connections."""

    escalated: bool = False


StopProgressCallback = Callable[[str, dict[str, object]], Awaitable[None]]


async def reap_on_ownership_transfer_failure(
    cleanup: Callable[[], Awaitable[object]],
    *,
    deadline_seconds: float = OWNERSHIP_TRANSFER_REAP_TIMEOUT_SECONDS,
) -> None:
    """Best-effort, cancellation-shielded cleanup before ownership transfers.

    Startup callers use this after catching ``BaseException`` so external task
    cancellation cannot strand a child between adapter, dispatch, and manager
    ownership. Foreground reap is bounded by ``deadline_seconds`` (default
    30s). The loop uses ``shield`` so repeated cancellation of the surrounding
    startup task does not abort an in-flight cleanup until the deadline.

    On expiry, logs a warning that foreground cleanup is abandoned and returns.
    Durable ``spawn_owned`` process scopes recorded on disk plus the reaper own
    any surviving child processes by construction.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_seconds
    try:
        cleanup_task = asyncio.ensure_future(cleanup())
    except BaseException:
        return
    while not cleanup_task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning(
                "Abandoning foreground ownership-transfer cleanup after %.1fs; "
                "durable spawn_owned scopes and reaper own any residue",
                deadline_seconds,
            )
            return
        try:
            await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=remaining)
        except TimeoutError:
            logger.warning(
                "Abandoning foreground ownership-transfer cleanup after %.1fs; "
                "durable spawn_owned scopes and reaper own any residue",
                deadline_seconds,
            )
            return
        except BaseException:  # cleanup must outlive repeated cancellation
            continue


@dataclass(frozen=True)
class HarnessEvent:
    """One parsed event from a running harness connection.

    Event type values stay in the producing harness namespace. Consumers that
    need semantic categories should normalize by both ``harness_id`` and
    ``event_type`` rather than assuming event-type names are globally unique.
    """

    event_type: str
    """Raw producer event name.

    Claude emits ``result`` plus SDK event types such as
    ``content_block_delta``, ``message_start``, ``message_stop``,
    ``content_block_start``, and ``content_block_stop``.

    Codex emits JSON-RPC method names such as ``turn/started``,
    ``turn/completed``, ``item/tool/completed``,
    ``item/tool/requestApproval``, and ``item/tool/requestUserInput``.

    OpenCode emits SSE event types such as ``session.idle``,
    ``session.error``, ``agent_message_chunk``, ``agent_thought_chunk``,
    ``tool_call``, and ``tool_call_update``.
    """
    payload: dict[str, object]
    """Parsed event payload from the harness transport."""
    harness_id: str
    """Identifier of the harness adapter that produced this event."""
    raw_text: str | None = None
    """Optional debug-level wire content, when retained by the adapter."""


@dataclass(frozen=True)
class HarnessRequest:
    """Inbound harness request that needs a policy decision."""

    request_id: str
    request_type: Literal["approval", "user_input"]
    method: str
    payload: dict[str, object]


class PrimaryRuntimeRequestPolicy(StrEnum):
    """Runner policy for runtime requests during primary-session launches."""

    NONE = "none"
    AUTO_ACCEPT = "auto_accept"
    SURFACE_EVENTS = "surface_events"


class PrimaryRuntimeEventSurface(StrEnum):
    """Where primary-session runtime requests should be surfaced."""

    NONE = "none"
    CONNECTION_EVENT_STREAM = "connection_event_stream"


class ServerRequestHandler(Protocol):
    """Policy boundary for inbound harness server requests."""

    async def handle_request(
        self,
        connection: HarnessConnection[Any],
        request: HarnessRequest,
    ) -> None:
        """Handle one inbound request without embedding policy in transport."""


class AutoAcceptHandler:
    """Default request policy: preserve existing non-chat auto-answer behavior."""

    no_runtime_hitl: bool = True

    async def handle_request(
        self,
        connection: HarnessConnection[Any],
        request: HarnessRequest,
    ) -> None:
        if request.request_type == "approval":
            await connection.respond_request(request.request_id, "accept")
            return
        if request.request_type == "user_input":
            await connection.respond_user_input(request.request_id, {})
            return
        raise ValueError(f"Unsupported harness request type: {request.request_type}")


class InteractiveHandler:
    """HITL request policy seam for externally resolved decisions.

    This handler surfaces harness requests as events instead of auto-answering
    them, then expects external code to respond later through the connection.
    It remains the policy boundary used by managed-primary flows that surface
    runtime requests (`request/opened`) to launch observers.
    """

    no_runtime_hitl: bool = False

    def __init__(
        self,
        event_sink: Callable[[HarnessEvent], Awaitable[None]],
    ) -> None:
        self._event_sink = event_sink

    async def handle_request(
        self,
        connection: HarnessConnection[Any],
        request: HarnessRequest,
    ) -> None:
        await self._event_sink(
            HarnessEvent(
                event_type="request/opened",
                payload={
                    "request_id": request.request_id,
                    "request_type": request.request_type,
                    "method": request.method,
                    "params": request.payload,
                },
                harness_id=connection.harness_id.value,
                raw_text=None,
            )
        )


@dataclass(frozen=True)
class ConnectionConfig:
    """Configuration inputs for starting one harness connection.

    ``child_env`` is the complete, sanitized environment resolved by the launch
    bind seam. Connections consume it as-is and may only add values that become
    knowable while establishing the connection.
    """

    spawn_id: SpawnId
    harness_id: HarnessId
    prompt: str
    control_root: Path
    child_env: dict[str, str]
    runtime_root: Path | None = None
    task_cwd: Path | None = None
    system: str | None = None
    timeout_seconds: float | None = None
    pi_child_wave_timeout_seconds: float | None = None
    resident_deadline_seconds: float | None = None
    resident_poll_seconds: float | None = None
    resident_rearm_budget: int | None = None
    pi_task_ping_interval_seconds: float | None = None
    pi_task_ping_reset_on_activity: bool | None = None
    ws_bind_host: str = "127.0.0.1"
    ws_port: int = 0
    pi_session_role: PiSessionRole | None = None
    debug_tracer: DebugTracer | None = None
    session_id_observer: Callable[[str], None] | None = None


def validate_prompt_size(config: ConnectionConfig) -> None:
    """Validate initial prompt size before contacting harness transport endpoints."""

    prompt_bytes = len(config.prompt.encode("utf-8"))
    if prompt_bytes > MAX_INITIAL_PROMPT_BYTES:
        raise PromptTooLargeError(
            actual_bytes=prompt_bytes,
            max_bytes=MAX_INITIAL_PROMPT_BYTES,
            harness=config.harness_id.value,
        )


class HarnessConnection(Generic[SpecT], ABC):
    """Single full-duplex connection contract for all harness transports."""

    @property
    @abstractmethod
    def state(self) -> ConnectionState: ...

    @property
    @abstractmethod
    def harness_id(self) -> HarnessId: ...

    @property
    @abstractmethod
    def spawn_id(self) -> SpawnId: ...

    @property
    @abstractmethod
    def capabilities(self) -> ConnectionCapabilities: ...

    @property
    @abstractmethod
    def session_id(self) -> str | None: ...

    @property
    def primary_event_scope(self) -> PrimaryEventScope | None:
        """Parent scope for events that multiplex child activity on the same stream."""
        return None

    @property
    @abstractmethod
    def subprocess_pid(self) -> int | None: ...

    @property
    def resident_backend(self) -> ResidentBackendControl | None:
        """Resident backend control seam, when this transport supports resident turns."""
        return None

    @property
    def scope_snapshot(self) -> ProcessScopeSnapshot | None:
        """Process scope facts for the managed backend subprocess, if any."""
        return None

    @abstractmethod
    async def start(self, config: ConnectionConfig, spec: SpecT) -> None: ...

    @property
    def observer_endpoint(self) -> ObserverEndpoint | None:
        """Attach endpoint for primary observer mode. None if not in observer mode."""
        return None

    async def start_observer(self, config: ConnectionConfig, spec: SpecT) -> None:
        """Start connection in primary observer mode.

        Subclasses that support observer mode must override this method
        to set their internal observer flag and then call start().
        """

        if not self.capabilities.supports_primary_observer:
            raise RuntimeError(f"{self.harness_id} does not support primary observer mode")
        await self.start(config, spec)

    def configure_primary_runtime_requests(
        self,
        *,
        policy: PrimaryRuntimeRequestPolicy,
        event_sink: Callable[[HarnessEvent], Awaitable[None]] | None = None,
        request_handler: ServerRequestHandler | None = None,
    ) -> None:
        """Configure runtime-request handling for one primary-session launch."""

        _ = event_sink, request_handler
        if policy is PrimaryRuntimeRequestPolicy.NONE:
            return
        raise NotImplementedError(
            f"{self.harness_id.value} does not support primary runtime request policy overrides"
        )

    async def inject_runtime_event(self, event: HarnessEvent) -> None:
        """Inject one synthesized runtime event into the connection event stream."""

        _ = event
        raise NotImplementedError(
            f"{self.harness_id.value} does not support runtime event injection"
        )

    async def respond_request(
        self,
        request_id: str,
        decision: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        """Send an approval or rejection response through the harness transport."""

        _ = request_id, decision, payload
        raise NotImplementedError(
            f"{self.harness_id.value} does not support runtime request responses"
        )

    async def respond_user_input(
        self,
        request_id: str,
        answers: dict[str, object],
    ) -> None:
        """Send user-input answers through the harness transport."""

        _ = request_id, answers
        raise NotImplementedError(f"{self.harness_id.value} does not support runtime user input")

    @abstractmethod
    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult: ...

    @abstractmethod
    def health(self) -> bool: ...

    @abstractmethod
    async def send_user_message(self, text: str) -> None: ...

    @abstractmethod
    async def send_cancel(self) -> None: ...

    @abstractmethod
    def events(self) -> AsyncIterator[HarnessEvent]: ...


__all__ = [
    "MAX_HARNESS_MESSAGE_BYTES",
    "MAX_INITIAL_PROMPT_BYTES",
    "AutoAcceptHandler",
    "ConnectionCapabilities",
    "ConnectionConfig",
    "ConnectionNotReady",
    "ConnectionState",
    "HarnessConnection",
    "HarnessEvent",
    "HarnessRequest",
    "InteractiveHandler",
    "ObserverEndpoint",
    "PiSessionRole",
    "PrimaryRuntimeEventSurface",
    "PrimaryRuntimeRequestPolicy",
    "PromptTooLargeError",
    "ServerRequestHandler",
    "StopProgressCallback",
    "StopResult",
    "validate_prompt_size",
]
