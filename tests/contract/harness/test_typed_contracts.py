"""Regression tests for typed harness contracts."""

from __future__ import annotations

import inspect
import re
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from meridian.lib.harness import ensure_bootstrap
from meridian.lib.harness.adapter import (
    ApprovalContract,
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
    TransportContract,
)
from meridian.lib.harness.bundle import get_connection_cls
from meridian.lib.harness.ids import HarnessId, TransportId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import (
    PermissionResolver,
    PreflightResult,
    ResolvedLaunchSpec,
    TerminalSurfaceMode,
)
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver

_REQUIRED_ABSTRACT_MEMBERS = {
    "contract",
    "id",
    "consumed_fields",
    "explicitly_ignored_fields",
    "resolve_launch_spec",
}
_EXPECTED_PROTOCOL_HELPERS = {"handled_fields", "preflight"}
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src" / "meridian"


def _required_protocol_attrs() -> set[str]:
    attrs = getattr(HarnessAdapter, "__protocol_attrs__", None)
    if attrs is not None:
        return {str(attr) for attr in attrs}

    protocol_attrs: set[str] = set()
    for name, member in inspect.getmembers(HarnessAdapter):
        if name.startswith("_"):
            continue
        if isinstance(member, property) or inspect.isfunction(member):
            protocol_attrs.add(name)
    return protocol_attrs


def _class_definition_sites(class_name: str) -> list[str]:
    pattern = re.compile(rf"^class {re.escape(class_name)}\b")
    matches: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.match(line):
                matches.append(f"{path.relative_to(_REPO_ROOT).as_posix()}:{line_number}")
    return matches


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


def test_s040_protocol_and_abc_required_member_sets_reconcile() -> None:
    protocol_attrs = _required_protocol_attrs()
    abstract_methods = set(BaseHarnessAdapter.__abstractmethods__)

    assert protocol_attrs >= _REQUIRED_ABSTRACT_MEMBERS
    assert abstract_methods == _REQUIRED_ABSTRACT_MEMBERS
    assert protocol_attrs - abstract_methods == _EXPECTED_PROTOCOL_HELPERS

    handled_fields_getter = BaseHarnessAdapter.handled_fields.fget
    assert handled_fields_getter is not None
    assert getattr(handled_fields_getter, "__isabstractmethod__", False) is False

    assert getattr(BaseHarnessAdapter.preflight, "__isabstractmethod__", False) is False


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


def _test_contract() -> HarnessContract:
    return HarnessContract(
        capabilities=HarnessCapabilities(
            terminal_surface_modes=(TerminalSurfaceMode.PTY_MEDIATED,),
            default_terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        ),
        transport=TransportContract(transport_ids=()),
        projection=ProjectionContract(
            launch_spec_cls="ResolvedLaunchSpec",
            mode=ProjectionMode.SYSTEM_FIELD_WITH_USER_TURN,
        ),
        extraction=ExtractionContract(session_observation_order=()),
        approval=ApprovalContract(),
        bootstrap=BootstrapContract(mode=BootstrapMode.SUBPROCESS_ONLY),
    )


def test_resolved_launch_spec_rejects_continue_fork_without_session_id() -> None:
    with pytest.raises(ValidationError, match=r"continue_fork=True requires continue_session_id"):
        ResolvedLaunchSpec(
            prompt="test",
            continue_fork=True,
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )


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


def test_harness_and_transport_ids_have_single_definition_sites() -> None:
    harness_id_sites = _class_definition_sites("HarnessId")
    transport_id_sites = _class_definition_sites("TransportId")

    assert len(harness_id_sites) == 1
    assert harness_id_sites[0].startswith("src/meridian/lib/harness/ids.py:")

    assert len(transport_id_sites) == 1
    assert transport_id_sites[0].startswith("src/meridian/lib/harness/ids.py:")


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
    expected_shared_policy_projection = {
        HarnessId.CLAUDE: True,
        HarnessId.CODEX: True,
        HarnessId.OPENCODE: False,
    }

    for harness_id, expects_shared_projection in expected_shared_policy_projection.items():
        connection_cls = get_connection_cls(harness_id, TransportId.STREAMING)
        connection = connection_cls()
        harness_contract = get_default_harness_registry().get_contract(harness_id)
        supports_runtime_responses = (
            "respond_request" in connection_cls.__dict__
            and "respond_user_input" in connection_cls.__dict__
        )

        if supports_runtime_responses:
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
