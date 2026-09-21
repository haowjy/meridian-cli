"""Version-dispatching OpenCode connection.

The harness registry holds exactly one connection class per transport. OpenCode
1.x and 2.x speak different server APIs, so the registered class resolves the
installed major version at ``start()`` and delegates to the matching transport:

- 1.x → :class:`OpenCodeV1Connection` (frozen legacy JSON API)
- 2.x → :class:`OpenCodeV2Connection` (``/api`` session + event stream)

Version comes from ``MERIDIAN_HARNESS_OPENCODE_VERSION`` in the child env when
set, otherwise from probing ``opencode --version``. Everything else is a
straight pass-through to the selected transport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    ObserverEndpoint,
    PrimaryRuntimeRequestPolicy,
    RawHarnessEvent,
    ServerRequestHandler,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.harness.connections.opencode_http import OpenCodeV1Connection
from meridian.lib.harness.connections.opencode_v2_http import OpenCodeV2Connection
from meridian.lib.harness.opencode_backend import resolve_opencode_version
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

if TYPE_CHECKING:
    from meridian.lib.harness.connections.resident_backend import ResidentBackendControl
    from meridian.lib.harness.semantics import EventSemantics, PrimaryEventScope
    from meridian.lib.platform.process_scope import ProcessScopeSnapshot

_VERSION_ENV = "MERIDIAN_HARNESS_OPENCODE_VERSION"


class OpenCodeConnection(HarnessConnection[ResolvedLaunchSpec]):
    """Resolve the OpenCode version, then defer to the matching transport."""

    _CAPABILITIES = OpenCodeV1Connection._CAPABILITIES

    def __init__(self, request_handler: ServerRequestHandler | None = None) -> None:
        self._impl: HarnessConnection[ResolvedLaunchSpec] | None = None
        self._request_handler = request_handler
        # Managed-primary callers configure the runtime-request policy before
        # ``start()`` resolves the version. Hold it until ``_select()`` builds the
        # transport, then apply it to that transport.
        self._primary_runtime_config: (
            tuple[
                PrimaryRuntimeRequestPolicy,
                Callable[[RawHarnessEvent], Awaitable[None]] | None,
                ServerRequestHandler | None,
            ]
            | None
        ) = None

    def _transport(self) -> HarnessConnection[ResolvedLaunchSpec]:
        if self._impl is None:
            raise RuntimeError("OpenCode connection has not been started")
        return self._impl

    def _resolve_version(self, config: ConnectionConfig) -> str:
        preference = config.child_env.get(_VERSION_ENV)
        return resolve_opencode_version(preference, binary="opencode")

    def _select(self, config: ConnectionConfig) -> HarnessConnection[ResolvedLaunchSpec]:
        if self._resolve_version(config) == "v2":
            impl: HarnessConnection[ResolvedLaunchSpec] = OpenCodeV2Connection(
                request_handler=self._request_handler
            )
        else:
            impl = OpenCodeV1Connection(request_handler=self._request_handler)
        primary_config = self._primary_runtime_config
        if primary_config is not None:
            policy, event_sink, request_handler = primary_config
            impl.configure_primary_runtime_requests(
                policy=policy,
                event_sink=event_sink,
                request_handler=request_handler,
            )
        return impl

    @property
    def state(self) -> ConnectionState:
        return self._impl.state if self._impl is not None else "created"

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.OPENCODE

    @property
    def spawn_id(self) -> SpawnId:
        return self._transport().spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        if self._impl is not None:
            return self._impl.capabilities
        return self._CAPABILITIES

    @property
    def session_id(self) -> str | None:
        return self._impl.session_id if self._impl is not None else None

    @property
    def primary_event_scope(self) -> PrimaryEventScope | None:
        return self._transport().primary_event_scope

    @property
    def subprocess_pid(self) -> int | None:
        return self._impl.subprocess_pid if self._impl is not None else None

    @property
    def scope_snapshot(self) -> ProcessScopeSnapshot | None:
        return self._impl.scope_snapshot if self._impl is not None else None

    @property
    def resident_backend(self) -> ResidentBackendControl | None:
        if self._impl is None:
            return None
        return self._impl.resident_backend

    @property
    def observer_endpoint(self) -> ObserverEndpoint | None:
        return self._impl.observer_endpoint if self._impl is not None else None

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        self._impl = self._select(config)
        await self._impl.start(config, spec)

    async def start_observer(
        self, config: ConnectionConfig, spec: ResolvedLaunchSpec
    ) -> None:
        self._impl = self._select(config)
        await self._impl.start_observer(config, spec)

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        return await self._transport().stop(reason=reason, progress=progress)

    def health(self) -> bool:
        if self._impl is None:
            return False
        return self._impl.health()

    async def send_user_message(self, text: str) -> None:
        await self._transport().send_user_message(text)

    async def send_cancel(self) -> None:
        await self._transport().send_cancel()

    def events(self) -> AsyncIterator[RawHarnessEvent]:
        return self._transport().events()

    def observe_event_semantics(self, semantics: EventSemantics) -> None:
        if self._impl is not None:
            self._impl.observe_event_semantics(semantics)

    def configure_primary_runtime_requests(
        self,
        *,
        policy: PrimaryRuntimeRequestPolicy,
        event_sink: Callable[[RawHarnessEvent], Awaitable[None]] | None = None,
        request_handler: ServerRequestHandler | None = None,
    ) -> None:
        self._primary_runtime_config = (policy, event_sink, request_handler)
        if self._impl is None:
            return
        self._impl.configure_primary_runtime_requests(
            policy=policy,
            event_sink=event_sink,
            request_handler=request_handler,
        )

    async def inject_runtime_event(self, event: RawHarnessEvent) -> None:
        await self._transport().inject_runtime_event(event)

    async def respond_request(
        self,
        request_id: str,
        decision: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        await self._transport().respond_request(request_id, decision, payload)

    async def respond_user_input(
        self,
        request_id: str,
        answers: dict[str, object],
    ) -> None:
        await self._transport().respond_user_input(request_id, answers)


__all__ = ["OpenCodeConnection"]
