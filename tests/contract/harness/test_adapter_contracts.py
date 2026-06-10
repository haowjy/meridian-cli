"""Contract tests for explicit harness adapter declarations."""

from __future__ import annotations

from meridian.lib.core.types import HarnessId, TransportId
from meridian.lib.harness import ensure_bootstrap
from meridian.lib.harness.adapter import (
    BootstrapMode,
    ForkMaterializationMode,
    PrelaunchBootstrapMode,
    ProjectionMode,
    RuntimeHitlMode,
    SessionSeedMode,
)
from meridian.lib.harness.bundle import (
    HarnessProjectionPorts,
    get_bundle_registry,
    get_connection_cls,
)
from meridian.lib.harness.connections.base import (
    PrimaryRuntimeEventSurface,
    PrimaryRuntimeRequestPolicy,
)
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import TerminalSurfaceMode


def test_harness_contracts_declare_terminal_surface_modes_and_bootstrap_modes() -> None:
    ensure_bootstrap()
    registry = get_default_harness_registry()

    claude = registry.get_contract(HarnessId.CLAUDE)
    assert claude.capabilities.terminal_surface_modes == (TerminalSurfaceMode.PTY_MEDIATED,)
    assert claude.capabilities.requires_initial_prompt is False
    assert claude.projection.mode is ProjectionMode.PROMPT_FILE_APPEND_SYSTEM
    assert claude.bootstrap.mode.value == "subprocess_only"
    assert claude.bootstrap.fork_materialization is ForkMaterializationMode.NATIVE_CONTINUE_FORK
    assert claude.bootstrap.primary_session_seed_mode is SessionSeedMode.PROJECTED_ARGS
    assert claude.bootstrap.streaming_session_seed_mode is SessionSeedMode.PROJECTED_ARGS
    assert (
        claude.bootstrap.prelaunch_bootstrap_mode
        is PrelaunchBootstrapMode.ENV_OVERLAY_AND_SESSION_ACCESS
    )
    assert claude.bootstrap.observer_controller is None

    for harness_id in (HarnessId.CODEX, HarnessId.OPENCODE):
        contract = registry.get_contract(harness_id)
        assert contract.capabilities.terminal_surface_modes == (
            TerminalSurfaceMode.PTY_MEDIATED,
            TerminalSurfaceMode.NATIVE_INHERIT,
        )
        assert contract.capabilities.default_terminal_surface_mode is (
            TerminalSurfaceMode.PTY_MEDIATED
        )
        assert contract.bootstrap.mode.value == "managed_primary_attach"
        assert contract.bootstrap.primary_session_seed_mode is SessionSeedMode.NONE
        assert contract.bootstrap.streaming_session_seed_mode is SessionSeedMode.NONE
        assert contract.bootstrap.prelaunch_bootstrap_mode is PrelaunchBootstrapMode.NONE
        assert contract.bootstrap.observer_controller is not None
        assert contract.transport.observer_controller_required is True
        assert contract.capabilities.requires_initial_prompt is (harness_id is HarnessId.CODEX)
        assert contract.projection.mode is ProjectionMode.SYSTEM_FIELD_WITH_USER_TURN

    cursor = registry.get_contract(HarnessId.CURSOR)
    assert cursor.projection.mode is ProjectionMode.POSITIONAL_PROMPT
    assert (
        registry.get_contract(HarnessId.CODEX).bootstrap.fork_materialization
        is ForkMaterializationMode.MERIDIAN_MATERIALIZED_FORK
    )
    assert (
        registry.get_contract(HarnessId.OPENCODE).bootstrap.fork_materialization
        is ForkMaterializationMode.NATIVE_CONTINUE_FORK
    )


def test_harness_contracts_match_registered_transport_maps() -> None:
    ensure_bootstrap()
    registry = get_default_harness_registry()

    expected_transport_ids = {
        HarnessId.CLAUDE: (TransportId.STREAMING,),
        HarnessId.CODEX: (TransportId.STREAMING,),
        HarnessId.CURSOR: (TransportId.SUBPROCESS,),
        HarnessId.OPENCODE: (TransportId.STREAMING,),
        HarnessId.PI: (TransportId.STREAMING,),
    }

    for harness_id in registry.ids():
        contract = registry.get_contract(harness_id)
        assert contract.transport.transport_ids == expected_transport_ids[harness_id]


def test_bundle_projection_ports_align_with_contract_bootstrap_modes() -> None:
    ensure_bootstrap()

    for _harness_id, bundle in get_bundle_registry().items():
        assert bundle.adapter.contract.projection.launch_spec_cls == bundle.spec_cls.__name__
        managed_primary = bundle.projections.managed_primary
        if bundle.adapter.contract.bootstrap.mode is BootstrapMode.MANAGED_PRIMARY_ATTACH:
            assert managed_primary is not None
        else:
            assert managed_primary is None
        assert isinstance(bundle.projections, HarnessProjectionPorts)


def test_opencode_contract_matches_current_runtime_hitl_and_permission_projection_limits() -> None:
    ensure_bootstrap()
    registry = get_default_harness_registry()

    contract = registry.get_contract(HarnessId.OPENCODE)
    connection = get_connection_cls(HarnessId.OPENCODE, TransportId.STREAMING)()

    assert contract.approval.runtime_hitl is RuntimeHitlMode.NONE
    assert contract.approval.default_runtime_request_policy == "none"
    assert contract.approval.primary_session_runtime_request_policy is (
        PrimaryRuntimeRequestPolicy.NONE
    )
    assert contract.approval.primary_session_runtime_event_surface is (
        PrimaryRuntimeEventSurface.NONE
    )
    assert contract.approval.subprocess_permission_flags_projected_by_shared_policy is False
    assert contract.bootstrap.primary_attach_failure_policy == "fallback_to_blackbox"
    assert connection.capabilities.supports_runtime_hitl is False


def test_codex_contract_declares_primary_runtime_event_surfacing() -> None:
    ensure_bootstrap()
    registry = get_default_harness_registry()

    contract = registry.get_contract(HarnessId.CODEX)

    assert contract.approval.runtime_hitl is RuntimeHitlMode.CONNECTION_REQUESTS
    assert contract.approval.primary_session_runtime_request_policy is (
        PrimaryRuntimeRequestPolicy.SURFACE_EVENTS
    )
    assert contract.approval.primary_session_runtime_event_surface is (
        PrimaryRuntimeEventSurface.CONNECTION_EVENT_STREAM
    )
    assert contract.bootstrap.primary_attach_failure_policy == "raise"

