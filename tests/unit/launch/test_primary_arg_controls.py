from __future__ import annotations

from typing import Any, cast

import pytest

from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.native_session_args import (
    NativeSessionSurface,
    NormalizedNativeSessionArgs,
    PrimaryArgControls,
    normalize_native_session_args,
)
from meridian.lib.launch.compiler import FieldProvenance, ProvenanceLevel
from meridian.lib.launch.launch_types import ResolvedLaunchRouting
from meridian.lib.launch.policies import (
    ModelSelectionContext,
    ResolvedLaunchPolicy,
    primary_arg_controls,
)
from meridian.lib.launch.resolve import ResolvedSkills


def _policy(
    *,
    model: str | None,
    source: Any = ProvenanceLevel.CLI,
    selection: ModelSelectionContext | None = None,
    execution_policy: ResolvedExecutionPolicy | None = None,
) -> ResolvedLaunchPolicy:
    return ResolvedLaunchPolicy(
        profile=None,
        model=model,
        harness=HarnessId.CLAUDE,
        adapter=cast("SubprocessHarness", object()),
        resolved_skills=ResolvedSkills(skill_names=(), loaded_skills=(), missing_skills=()),
        routing=ResolvedLaunchRouting(model=model, harness=HarnessId.CLAUDE, agent=None),
        execution_policy=execution_policy or ResolvedExecutionPolicy(effort=""),
        field_provenance=FieldProvenance(model_source=source),  # type: ignore[arg-type]
        model_selection=selection,
    )


def test_primary_controls_preserve_retained_policy_and_native_alias_token() -> None:
    execution_policy = ResolvedExecutionPolicy(effort="")
    policy = _policy(
        model="provider/canonical",
        selection=ModelSelectionContext(
            requested_token="friendly-alias",
            selected_model_token="friendly-alias",
            canonical_model_id="provider/canonical",
            harness_provenance="bundle",
            harness_model_id="native-model-token",
        ),
        execution_policy=execution_policy,
    )

    controls = primary_arg_controls(policy)

    assert controls == PrimaryArgControls("native-model-token", True, execution_policy)
    assert controls.execution_policy is execution_policy


def test_primary_controls_normalize_whitespace_only_default_as_unselected() -> None:
    policy = _policy(
        model="   ",
        selection=ModelSelectionContext("", "", "  ", "harness-default", None),
    )

    controls = primary_arg_controls(policy)

    assert controls.model == ""
    assert controls.model_controlled is False


def test_primary_controls_do_not_authorize_whitespace_canonical_with_native_token() -> None:
    policy = _policy(
        model="   ",
        selection=ModelSelectionContext("", "", "  ", "harness-default", "native-model"),
    )

    controls = primary_arg_controls(policy)

    assert controls.model == "native-model"
    assert controls.model_controlled is False


def test_primary_controls_reject_whitespace_harness_native_token() -> None:
    policy = _policy(
        model="canonical",
        selection=ModelSelectionContext("alias", "alias", "canonical", "bundle", "  "),
    )

    controls = primary_arg_controls(policy)

    assert controls.model == "  "
    assert controls.model_controlled is False


@pytest.mark.parametrize(
    ("model", "source", "selection", "expected"),
    [
        ("model-a", ProvenanceLevel.UNSET, None, False),
        ("model-a", "unknown", None, False),
        ("model-a", ProvenanceLevel.CLI, None, True),
        (None, ProvenanceLevel.PROFILE_DEFAULT, None, True),
        (
            "canonical",
            ProvenanceLevel.CLI,
            ModelSelectionContext("alias", "alias", "different", "bundle", "native"),
            False,
        ),
        (
            "canonical",
            ProvenanceLevel.CLI,
            ModelSelectionContext("alias", "alias", "canonical", "bundle", None),
            False,
        ),
    ],
)
def test_model_control_requires_known_consistent_provenance(
    model: str | None,
    source: object,
    selection: ModelSelectionContext | None,
    expected: bool,
) -> None:
    assert (
        primary_arg_controls(
            _policy(model=model, source=source, selection=selection)
        ).model_controlled
        is expected
    )


def test_optional_controls_do_not_break_legacy_syntax_only_callable() -> None:
    def legacy(args: tuple[str, ...], surface: NativeSessionSurface) -> NormalizedNativeSessionArgs:
        assert surface == "managed"
        return NormalizedNativeSessionArgs(None, args)

    assert normalize_native_session_args(("x",), "managed", legacy).remaining_args == ("x",)
    with pytest.raises(TypeError, match="controls"):
        normalize_native_session_args(
            (),
            "managed",
            legacy,
            controls=PrimaryArgControls(None, False, ResolvedExecutionPolicy()),
        )


def test_normalizer_helper_forwards_nonempty_controls() -> None:
    controls = PrimaryArgControls(None, False, ResolvedExecutionPolicy())

    def aware(
        args: tuple[str, ...],
        surface: NativeSessionSurface,
        *,
        controls: PrimaryArgControls | None = None,
    ) -> NormalizedNativeSessionArgs:
        assert surface == "managed"
        assert controls is not None
        return NormalizedNativeSessionArgs(None, args)

    result = normalize_native_session_args((), "managed", aware, controls=controls)
    assert result.remaining_args == ()
