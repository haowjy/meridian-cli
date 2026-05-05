"""Backend acquisition strategy boundary for chat sessions."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

from meridian.lib.chat.backend_handle import BackendHandle
from meridian.lib.chat.event_observer import ChatEventObserver
from meridian.lib.chat.event_pipeline import ChatEventPipeline
from meridian.lib.chat.normalization.base import EventNormalizer
from meridian.lib.chat.protocol import CHAT_CONFIGURED, ChatEvent, utc_now_iso
from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.harness.ids import HarnessId
from meridian.lib.harness.launch_spec import ClaudeLaunchSpec
from meridian.lib.launch.context import ChildEnvContext
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.streaming.drain_policy import PersistentDrainPolicy

if TYPE_CHECKING:
    from meridian.lib.chat.runtime import PipelineLookup


class BackendAcquisition(Protocol):
    """Strategy for acquiring a backing execution on first prompt."""

    async def acquire(
        self,
        chat_id: str,
        initial_prompt: str,
        *,
        execution_generation: int = 0,
        chat_state: str = "active",
    ) -> BackendHandle:
        """Acquire a backend and send the initial prompt as part of startup."""
        ...


class BackendAcquisitionFactory(Protocol):
    """Build backend acquisition after the runtime pipeline lookup exists."""

    def build(
        self,
        *,
        pipeline_lookup: PipelineLookup,
        project_root: Path,
        runtime_root: Path,
    ) -> BackendAcquisition: ...


class _SpawnManagerLike(Protocol):
    async def start_spawn(
        self,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
        *,
        drain_policy: object | None = None,
        on_event: object | None = None,
    ) -> HarnessConnection[Any]: ...

    def register_observer(self, spawn_id: SpawnId, observer: object) -> None: ...

    def unregister_observer(self, spawn_id: SpawnId, observer: object) -> None: ...

    async def start_heartbeat(self, spawn_id: SpawnId) -> None: ...

    async def stop_spawn(self, spawn_id: SpawnId) -> None: ...


NormalizerFactory = Callable[[str, str], EventNormalizer]
ConnectionConfigFactory = Callable[[str, str], ConnectionConfig]
LaunchSpecFactory = Callable[[str], ResolvedLaunchSpec]


class ColdSpawnAcquisition:
    """Acquire a chat backend by starting a fresh SpawnManager execution.

    This is intentionally a narrow vertical slice: callers may inject factories
    for production preparation or tests, while this class owns the invariant that
    the chat observer is registered before the spawn starts and that chat spawns
    use ``PersistentDrainPolicy``.
    """

    def __init__(
        self,
        *,
        spawn_manager: _SpawnManagerLike,
        normalizer_factory: NormalizerFactory,
        pipeline_lookup: PipelineLookup,
        connection_config_factory: ConnectionConfigFactory | None = None,
        launch_spec_factory: LaunchSpecFactory | None = None,
        project_root: Path | None = None,
        runtime_root: Path | None = None,
        harness_id: HarnessId = HarnessId.CLAUDE,
    ) -> None:
        self._spawn_manager = spawn_manager
        self._normalizer_factory = normalizer_factory
        self._pipeline_lookup = pipeline_lookup
        self._connection_config_factory = connection_config_factory
        self._launch_spec_factory = launch_spec_factory
        self._project_root = project_root if project_root is not None else Path.cwd()
        self._runtime_root = runtime_root
        self._harness_id = harness_id

    async def acquire(
        self,
        chat_id: str,
        initial_prompt: str,
        *,
        execution_generation: int = 0,
        chat_state: str = "active",
    ) -> BackendHandle:
        """Start a cold backend with the first prompt and return its handle."""

        config = self._build_connection_config(chat_id, initial_prompt)
        spec = self._build_launch_spec(initial_prompt)
        execution_id = str(config.spawn_id)
        normalizer = self._build_normalizer(chat_id, execution_id)
        pipeline = self._build_pipeline(chat_id)
        observer = ChatEventObserver(
            normalizer=normalizer,
            pipeline=pipeline,
            execution_id=execution_id,
            execution_generation=execution_generation,
        )

        # Critical D15/D19 invariant: observer first, then SpawnManager start.
        self._spawn_manager.register_observer(config.spawn_id, observer)
        try:
            connection = await self._spawn_manager.start_spawn(
                config,
                spec,
                drain_policy=PersistentDrainPolicy(),
            )
            await pipeline.ingest(
                ChatEvent(
                    type=CHAT_CONFIGURED,
                    seq=0,
                    chat_id=chat_id,
                    execution_id=execution_id,
                    timestamp=utc_now_iso(),
                    payload=_configuration_payload(
                        harness=config.harness_id.value,
                        model=spec.model,
                        state=chat_state,
                        supports_hitl=connection.capabilities.supports_runtime_hitl,
                        supports_checkpoints=True,
                        supports_model_swap=connection.capabilities.runtime_model_switch,
                        supports_effort_swap=False,
                    ),
                    harness_id=config.harness_id.value,
                )
            )
            await self._spawn_manager.start_heartbeat(config.spawn_id)
        except Exception:
            with suppress(Exception):
                self._spawn_manager.unregister_observer(config.spawn_id, observer)
            with suppress(Exception):
                await self._spawn_manager.stop_spawn(config.spawn_id)
            raise
        return BackendHandle(
            spawn_id=config.spawn_id,
            spawn_manager=cast("Any", self._spawn_manager),
            connection=connection,
            execution_generation=execution_generation,
        )

    def _build_connection_config(self, chat_id: str, initial_prompt: str) -> ConnectionConfig:
        if self._connection_config_factory is not None:
            return self._connection_config_factory(chat_id, initial_prompt)
        spawn_id = _spawn_id()
        return ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=self._harness_id,
            prompt=initial_prompt,
            project_root=self._project_root,
            env_overrides=self._build_env_overrides(spawn_id),
        )

    def _build_env_overrides(self, spawn_id: SpawnId) -> dict[str, str]:
        env_overrides: dict[str, str] = {}
        if self._runtime_root is not None:
            env_overrides.update(
                ChildEnvContext.from_environment(
                    project_paths=ProjectConfigPaths(
                        project_root=self._project_root,
                        execution_cwd=self._project_root,
                    ),
                    runtime_root=self._runtime_root,
                ).child_context(child_spawn_id=str(spawn_id))
            )
        env_overrides["MERIDIAN_HARNESS"] = self._harness_id.value
        return env_overrides

    def _build_launch_spec(self, initial_prompt: str) -> ResolvedLaunchSpec:
        if self._launch_spec_factory is not None:
            return self._launch_spec_factory(initial_prompt)
        if self._harness_id == HarnessId.CLAUDE:
            return ClaudeLaunchSpec(
                prompt=initial_prompt,
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            )
        return ResolvedLaunchSpec(
            prompt=initial_prompt,
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )

    def _build_normalizer(
        self,
        chat_id: str,
        execution_id: str,
    ) -> EventNormalizer:
        return self._normalizer_factory(chat_id, execution_id)

    def _build_pipeline(self, chat_id: str) -> ChatEventPipeline:
        pipeline = self._pipeline_lookup.get_pipeline(chat_id)
        if pipeline is None:
            raise RuntimeError(f"chat pipeline not configured for {chat_id}")
        return pipeline


def _spawn_id() -> SpawnId:
    return SpawnId(f"chat-{uuid4()}")


def _configuration_payload(
    *,
    harness: str,
    model: str | None,
    state: str,
    supports_hitl: bool,
    supports_checkpoints: bool,
    supports_model_swap: bool,
    supports_effort_swap: bool,
) -> dict[str, object]:
    return {
        "harness": harness,
        "model": model or "",
        "state": state,
        "supports_hitl": supports_hitl,
        "supports_checkpoints": supports_checkpoints,
        "supports_model_swap": supports_model_swap,
        "supports_effort_swap": supports_effort_swap,
    }


__all__ = ["BackendAcquisition", "BackendAcquisitionFactory", "ColdSpawnAcquisition"]
