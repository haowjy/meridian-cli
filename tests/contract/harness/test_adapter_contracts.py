"""Contract tests for explicit harness adapter declarations."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.harness import HARNESS_EXTENSION_TOUCHPOINTS, ensure_bootstrap
from meridian.lib.harness.adapter import (
    BootstrapMode,
    ForkMaterializationMode,
    PrelaunchBootstrapMode,
    RuntimeHitlMode,
    SessionSeedMode,
)
from meridian.lib.harness.bundle import (
    HarnessProjectionPorts,
    get_bundle_registry,
    get_connection_cls,
    project_managed_primary_backend_command,
    project_managed_primary_bootstrap,
    project_subprocess_spec,
)
from meridian.lib.harness.connections.base import (
    PrimaryRuntimeEventSurface,
    PrimaryRuntimeRequestPolicy,
)
from meridian.lib.harness.ids import HarnessId, TransportId
from meridian.lib.harness.launch_spec import ClaudeLaunchSpec
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import TerminalSurfaceMode
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def test_harness_contracts_declare_terminal_surface_modes_and_bootstrap_modes() -> None:
    ensure_bootstrap()
    registry = get_default_harness_registry()

    claude = registry.get_contract(HarnessId.CLAUDE)
    assert claude.capabilities.terminal_surface_modes == (
        TerminalSurfaceMode.PTY_MEDIATED,
    )
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

    for harness_id in registry.ids():
        contract = registry.get_contract(harness_id)
        assert contract.transport.transport_ids == (TransportId.STREAMING,)


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


def test_bundle_projection_helpers_reject_spec_mismatch() -> None:
    ensure_bootstrap()

    spec = ClaudeLaunchSpec(
        prompt="hi",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(TypeError, match="expected CodexLaunchSpec"):
        project_subprocess_spec(HarnessId.CODEX, spec, base_command=("codex",))

    with pytest.raises(TypeError, match="expected OpenCodeLaunchSpec"):
        project_managed_primary_backend_command(
            HarnessId.OPENCODE,
            spec,
            host="127.0.0.1",
            port=9999,
        )

    with pytest.raises(TypeError, match="expected OpenCodeLaunchSpec"):
        project_managed_primary_bootstrap(
            HarnessId.OPENCODE,
            spec,
            project_root=Path("."),
        )


def test_harness_extension_touchpoints_document_contract_edit_set() -> None:
    assert any("HarnessContract" in touchpoint for touchpoint in HARNESS_EXTENSION_TOUCHPOINTS)
