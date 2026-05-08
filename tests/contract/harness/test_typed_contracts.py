"""Regression tests for typed harness contracts."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
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
)
from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.harness.connections.codex_ws import CodexConnection
from meridian.lib.harness.connections.opencode_http import OpenCodeConnection
from meridian.lib.harness.extractors.base import HarnessExtractor
from meridian.lib.harness.ids import HarnessId, TransportId
from meridian.lib.harness.launch_spec import ClaudeLaunchSpec, CodexLaunchSpec
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import (
    PermissionResolver,
    PreflightResult,
    ResolvedLaunchSpec,
    TerminalSurfaceMode,
)
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_s001_base_adapter_requires_resolve_launch_spec_override() -> None:
    class NewHarness(BaseHarnessAdapter[ResolvedLaunchSpec]):
        pass

    with pytest.raises(TypeError, match=r"resolve_launch_spec"):
        NewHarness()


class _MissingResolveLaunchSpecHarness(BaseHarnessAdapter[ResolvedLaunchSpec]):
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


def test_s001_missing_only_resolve_launch_spec_mentions_that_method() -> None:
    with pytest.raises(TypeError) as exc_info:
        _MissingResolveLaunchSpecHarness()

    message = str(exc_info.value)
    assert "resolve_launch_spec" in message
    assert "contract" not in message
    assert "id" not in message
    assert "consumed_fields" not in message
    assert "explicitly_ignored_fields" not in message


def test_registered_adapters_satisfy_runtime_harness_protocols() -> None:
    ensure_bootstrap()
    registry = get_default_harness_registry()

    for harness_id in registry.ids():
        adapter = registry.get(harness_id)
        assert isinstance(adapter, HarnessAdapter)
        assert isinstance(adapter, SubprocessHarness)
        assert registry.get_subprocess_harness(harness_id) is adapter


class _MissingIdHarness(BaseHarnessAdapter[ResolvedLaunchSpec]):
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
        _ = run, perms
        return ResolvedLaunchSpec(
            prompt="test",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )


def test_s040_missing_id_raises_instantiation_error() -> None:
    with pytest.raises(TypeError, match=r"\bid\b"):
        _MissingIdHarness()


class _IncompleteHarness(BaseHarnessAdapter[ResolvedLaunchSpec]):
    def resolve_launch_spec(
        self,
        run: SpawnParams,
        perms: PermissionResolver,
    ) -> ResolvedLaunchSpec:
        _ = run, perms
        return ResolvedLaunchSpec(
            prompt="test",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )


def test_s040_incomplete_adapter_lists_all_missing_members() -> None:
    with pytest.raises(TypeError) as exc_info:
        _IncompleteHarness()

    message = str(exc_info.value)
    assert "contract" in message
    assert "id" in message
    assert "consumed_fields" in message
    assert "explicitly_ignored_fields" in message


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


def test_handled_fields_unions_consumed_and_ignored() -> None:
    assert _CompleteHarness().handled_fields == frozenset({"prompt"})


class _ManagedPrimaryHarness(_CompleteHarness):
    @property
    def id(self) -> HarnessId:
        return HarnessId.CODEX

    @property
    def contract(self) -> HarnessContract:
        return _test_contract(
            launch_spec_cls="CodexLaunchSpec",
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

    async def stop(self) -> None:
        return None

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

    bundle = _make_bundle(
        harness_id=HarnessId.CLAUDE,
        adapter=_CompleteHarness(),
        spec_cls=ClaudeLaunchSpec,
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
        spec_cls=CodexLaunchSpec,
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


def test_preflight_result_build_wraps_extra_env_in_mapping_proxy() -> None:
    extra_env = {"MERIDIAN_FLAG": "1"}
    result = PreflightResult.build(
        expanded_passthrough_args=("--json",),
        extra_env=extra_env,
    )

    assert isinstance(result.extra_env, MappingProxyType)
    assert dict(result.extra_env) == extra_env

    extra_env["MUTATED_LATER"] = "2"
    assert "MUTATED_LATER" not in result.extra_env

    with pytest.raises(TypeError):
        result.extra_env["NEW_FLAG"] = "3"  # type: ignore[index]


def test_leaf_imports_do_not_form_a_cycle() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import meridian.lib.launch.launch_types; "
                "import meridian.lib.harness.adapter; "
                "import meridian.lib.harness.connections.base"
            ),
        ],
        capture_output=True,
        check=False,
        cwd=_REPO_ROOT,
        text=True,
    )

    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )


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
        else:
            assert harness_contract.approval.runtime_hitl is RuntimeHitlMode.NONE
            assert harness_contract.approval.default_runtime_request_policy == "none"
            assert connection.capabilities.supports_runtime_hitl is False

        assert (
            harness_contract.approval.subprocess_permission_flags_projected_by_shared_policy
            is expects_shared_projection
        )
