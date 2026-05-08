"""Contract tests for explicit harness adapter declarations."""

from __future__ import annotations

from meridian.lib.harness import HARNESS_EXTENSION_TOUCHPOINTS, ensure_bootstrap
from meridian.lib.harness.adapter import RuntimeHitlMode
from meridian.lib.harness.bundle import get_connection_cls
from meridian.lib.harness.ids import HarnessId, TransportId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import TerminalSurfaceMode


def test_harness_contracts_declare_terminal_surface_modes_and_bootstrap_modes() -> None:
    ensure_bootstrap()
    registry = get_default_harness_registry()

    claude = registry.get_contract(HarnessId.CLAUDE)
    assert claude.capabilities.terminal_surface_modes == (
        TerminalSurfaceMode.PTY_MEDIATED,
    )
    assert claude.bootstrap.mode.value == "subprocess_only"
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
        assert contract.bootstrap.observer_controller is not None
        assert contract.transport.observer_controller_required is True


def test_harness_contracts_match_registered_transport_maps() -> None:
    ensure_bootstrap()
    registry = get_default_harness_registry()

    for harness_id in registry.ids():
        contract = registry.get_contract(harness_id)
        assert contract.transport.transport_ids == (TransportId.STREAMING,)


def test_opencode_contract_matches_current_runtime_hitl_and_permission_projection_limits() -> None:
    ensure_bootstrap()
    registry = get_default_harness_registry()

    contract = registry.get_contract(HarnessId.OPENCODE)
    connection = get_connection_cls(HarnessId.OPENCODE, TransportId.STREAMING)()

    assert contract.approval.runtime_hitl is RuntimeHitlMode.NONE
    assert contract.approval.default_runtime_request_policy == "none"
    assert contract.approval.subprocess_permission_flags_projected_by_shared_policy is False
    assert connection.capabilities.supports_runtime_hitl is False


def test_harness_extension_touchpoints_document_contract_edit_set() -> None:
    assert any("HarnessContract" in touchpoint for touchpoint in HARNESS_EXTENSION_TOUCHPOINTS)
