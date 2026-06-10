"""Regression tests for typed harness contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import HarnessId, SpawnId, TransportId
from meridian.lib.harness import ensure_bootstrap
from meridian.lib.harness.adapter import (
    ApprovalContract,
    ArtifactStore,
    BaseHarnessAdapter,
    BootstrapContract,
    BootstrapMode,
    ExtractionContract,
    HarnessAdapter,
    HarnessCapabilities,
    HarnessContract,
    ProjectionContract,
    ProjectionMode,
    RuntimeHitlMode,
    SpawnParams,
    SubprocessHarness,
    TransportContract,
)
from meridian.lib.harness.bundle import (
    HarnessBundle,
    HarnessProjectionPorts,
    ManagedPrimaryProjectionPorts,
    get_connection_cls,
    register_harness_bundle,
)
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    PrimaryRuntimeEventSurface,
    PrimaryRuntimeRequestPolicy,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.harness.connections.codex_ws import CodexConnection
from meridian.lib.harness.connections.opencode_http import OpenCodeConnection
from meridian.lib.harness.extractors.base import HarnessExtractor
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import (
    PermissionResolver,
    ResolvedLaunchSpec,
    TerminalSurfaceMode,
)
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def test_registered_adapters_satisfy_runtime_harness_protocols() -> None:
    ensure_bootstrap()
    registry = get_default_harness_registry()

    for harness_id in registry.ids():
        adapter = registry.get(harness_id)
        assert isinstance(adapter, HarnessAdapter)
        assert isinstance(adapter, SubprocessHarness)
        assert registry.get_subprocess_harness(harness_id) is adapter


class _CompleteHarness(BaseHarnessAdapter[ResolvedLaunchSpec]):
    @property
    def id(self) -> HarnessId:
        return HarnessId.CLAUDE

    @property
    def contract(self) -> HarnessContract:
        return _test_contract()

    @property
    def consumed_fields(self) -> frozenset[str]:
        return frozenset({"prompt"})

    @property
    def explicitly_ignored_fields(self) -> frozenset[str]:
        return frozenset()

    def resolve_launch_spec(
        self,
        run: SpawnParams,
        perms: PermissionResolver,
    ) -> ResolvedLaunchSpec:
        _ = perms
        return ResolvedLaunchSpec(
            prompt=run.prompt,
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )


class _ManagedPrimaryHarness(_CompleteHarness):
    @property
    def id(self) -> HarnessId:
        return HarnessId.CODEX

    @property
    def contract(self) -> HarnessContract:
        return _test_contract(
            launch_spec_cls="ResolvedLaunchSpec",
            bootstrap_mode=BootstrapMode.MANAGED_PRIMARY_ATTACH,
        )


class _MinimalConnection(HarnessConnection[ResolvedLaunchSpec]):
    @property
    def state(self) -> ConnectionState:
        return "created"

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CLAUDE

    @property
    def spawn_id(self) -> SpawnId:
        return SpawnId("p-contract")

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=False,
            supports_cancel=False,
            runtime_model_switch=False,
            structured_reasoning=False,
        )

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def subprocess_pid(self) -> int | None:
        return None

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = config, spec

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        return StopResult()

    def health(self) -> bool:
        return True

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self) -> AsyncIterator[HarnessEvent]:
        if False:
            yield HarnessEvent(event_type="never", payload={}, harness_id="claude")


class _MinimalExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    def detect_session_id_from_event(self, event: HarnessEvent) -> str | None:
        _ = event
        return None

    def detect_session_id_from_artifacts(
        self,
        *,
        spec: ResolvedLaunchSpec,
        launch_env: dict[str, str],
        child_cwd: Path,
        runtime_root: Path,
    ) -> str | None:
        _ = spec, launch_env, child_cwd, runtime_root
        return None

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        _ = artifacts, spawn_id
        return TokenUsage()

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        _ = artifacts, spawn_id
        return None

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        _ = artifacts, spawn_id
        return None


def _test_contract(
    *,
    launch_spec_cls: str = "ResolvedLaunchSpec",
    bootstrap_mode: BootstrapMode = BootstrapMode.SUBPROCESS_ONLY,
) -> HarnessContract:
    return HarnessContract(
        capabilities=HarnessCapabilities(
            terminal_surface_modes=(TerminalSurfaceMode.PTY_MEDIATED,),
            default_terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        ),
        transport=TransportContract(transport_ids=(TransportId.STREAMING,)),
        projection=ProjectionContract(
            launch_spec_cls=launch_spec_cls,
            mode=ProjectionMode.SYSTEM_FIELD_WITH_USER_TURN,
        ),
        extraction=ExtractionContract(session_observation_order=()),
        approval=ApprovalContract(),
        bootstrap=BootstrapContract(mode=bootstrap_mode),
    )


def _make_bundle(
    *,
    harness_id: HarnessId,
    adapter: BaseHarnessAdapter[ResolvedLaunchSpec],
    spec_cls: type[ResolvedLaunchSpec],
    managed_primary: ManagedPrimaryProjectionPorts[ResolvedLaunchSpec, object] | None = None,
) -> HarnessBundle[ResolvedLaunchSpec]:
    return HarnessBundle(
        harness_id=harness_id,
        adapter=adapter,
        spec_cls=spec_cls,
        extractor=_MinimalExtractor(),
        connections={TransportId.STREAMING: _MinimalConnection},
        projections=HarnessProjectionPorts(
            subprocess_cli_args=lambda spec, *, base_command: [*base_command, spec.prompt],
            managed_primary=managed_primary,
        ),
    )


def test_resolved_launch_spec_rejects_continue_fork_without_session_id() -> None:
    with pytest.raises(ValidationError, match=r"continue_fork=True requires continue_session_id"):
        ResolvedLaunchSpec(
            prompt="test",
            continue_fork=True,
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )


def test_register_harness_bundle_rejects_projection_spec_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.harness.bundle as bundle_module

    monkeypatch.setattr(bundle_module, "_REGISTRY", {})

    # A custom subclass has a different __name__, so it mismatches the contract
    # which declares launch_spec_cls="ResolvedLaunchSpec".
    class _CustomSpec(ResolvedLaunchSpec):
        pass

    bundle = _make_bundle(
        harness_id=HarnessId.CLAUDE,
        adapter=_CompleteHarness(),
        spec_cls=_CustomSpec,
    )

    with pytest.raises(ValueError, match="projection launch_spec_cls"):
        register_harness_bundle(bundle)


def test_register_harness_bundle_rejects_managed_primary_port_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.harness.bundle as bundle_module

    monkeypatch.setattr(bundle_module, "_REGISTRY", {})

    managed_primary_ports = ManagedPrimaryProjectionPorts(
        backend_command=lambda spec, *, host, port: ["backend", host, str(port), spec.prompt],
        bootstrap_payload=lambda spec, *, project_root: {
            "project_root": project_root.as_posix(),
            "prompt": spec.prompt,
        },
    )

    codex_without_managed_primary = _make_bundle(
        harness_id=HarnessId.CODEX,
        adapter=_ManagedPrimaryHarness(),
        spec_cls=ResolvedLaunchSpec,
    )
    with pytest.raises(ValueError, match="requires managed-primary projection ports"):
        register_harness_bundle(codex_without_managed_primary)

    claude_with_managed_primary = _make_bundle(
        harness_id=HarnessId.CLAUDE,
        adapter=_CompleteHarness(),
        spec_cls=ResolvedLaunchSpec,
        managed_primary=managed_primary_ports,
    )
    with pytest.raises(ValueError, match="registered managed-primary projection ports"):
        register_harness_bundle(claude_with_managed_primary)


def test_runtime_hitl_contracts_align_with_default_connection_capabilities() -> None:
    ensure_bootstrap()
    registry = get_default_harness_registry()
    expected_shared_policy_projection = {
        HarnessId.CLAUDE: True,
        HarnessId.CODEX: True,
        HarnessId.OPENCODE: False,
    }
    expected_connection_types = {
        HarnessId.CLAUDE: ClaudeConnection,
        HarnessId.CODEX: CodexConnection,
        HarnessId.OPENCODE: OpenCodeConnection,
    }

    for harness_id, expects_shared_projection in expected_shared_policy_projection.items():
        connection_cls = get_connection_cls(harness_id, TransportId.STREAMING)
        connection = connection_cls()
        assert connection_cls is expected_connection_types[harness_id]
        harness_contract = registry.get_contract(harness_id)

        overrides_runtime_hitl_responses = (
            connection_cls.respond_request is not HarnessConnection.respond_request
            and connection_cls.respond_user_input is not HarnessConnection.respond_user_input
        )

        if overrides_runtime_hitl_responses:
            assert harness_contract.approval.runtime_hitl is RuntimeHitlMode.CONNECTION_REQUESTS
            assert harness_contract.approval.default_runtime_request_policy == "auto_accept"
            assert harness_contract.approval.primary_session_runtime_request_policy is (
                PrimaryRuntimeRequestPolicy.SURFACE_EVENTS
            )
            assert harness_contract.approval.primary_session_runtime_event_surface is (
                PrimaryRuntimeEventSurface.CONNECTION_EVENT_STREAM
            )
        else:
            assert harness_contract.approval.runtime_hitl is RuntimeHitlMode.NONE
            assert harness_contract.approval.default_runtime_request_policy == "none"
            assert harness_contract.approval.primary_session_runtime_request_policy is (
                PrimaryRuntimeRequestPolicy.NONE
            )
            assert harness_contract.approval.primary_session_runtime_event_surface is (
                PrimaryRuntimeEventSurface.NONE
            )
            assert connection.capabilities.supports_runtime_hitl is False

        assert (
            harness_contract.approval.subprocess_permission_flags_projected_by_shared_policy
            is expects_shared_projection
        )
