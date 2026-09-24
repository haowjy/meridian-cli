"""Pi primary scalar admission matches the native TUI projector's ownership."""

from __future__ import annotations

import pytest

from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.harness.native_session_args import PrimaryArgControls
from meridian.lib.harness.pi_native_source import normalize_pi_primary_session_args
from meridian.lib.harness.projections.project_pi_common import project_pi_thinking_level
from meridian.lib.harness.projections.project_pi_native_tui import (
    project_pi_native_tui_spec_to_cli_args,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver


def _controls(*, model: str | None = "native/model", controlled: bool = True,
              effort: str | None = "low") -> PrimaryArgControls:
    return PrimaryArgControls(
        model=model,
        model_controlled=controlled,
        execution_policy=ResolvedExecutionPolicy(effort=effort),
    )


def _project(
    args: tuple[str, ...], *, model: str = "native/model", effort: str = "low"
) -> list[str]:
    return project_pi_native_tui_spec_to_cli_args(
        ResolvedLaunchSpec(
            harness="pi",
            model=model,
            effort=effort,
            permission_resolver=TieredPermissionResolver(config=PermissionConfig()),
            extra_args=args,
        ),
        base_command=("pi",),
    )


def test_pi_tui_effort_mapping_is_shared_with_argument_admission() -> None:
    assert project_pi_thinking_level("low") == "minimal"
    raw = ("--thinking", "high")
    normalized = normalize_pi_primary_session_args(
        raw, "subprocess", controls=_controls(effort="low")
    )
    assert normalized.remaining_args == ()
    assert normalized.warnings == (
        "Ignored raw thinking option; Meridian's resolved effort takes precedence.",
    )
    projected = _project(normalized.remaining_args)
    assert projected[projected.index("--thinking") + 1] == "minimal"


@pytest.mark.parametrize(
    "args",
    [
        ("--model", "raw/alias", "--thinking", "high"),
        ("--thinking", "high", "-m", "raw/alias"),
    ],
)
def test_pi_model_alias_and_effort_duplicates_are_suppressed_in_either_order(
    args: tuple[str, ...],
) -> None:
    normalized = normalize_pi_primary_session_args(
        args, "subprocess", controls=_controls()
    )
    assert normalized.remaining_args == ()
    assert len(normalized.warnings) == 2
    projected = _project(normalized.remaining_args)
    assert projected[projected.index("--model") + 1] == "native/model"
    assert projected[projected.index("--thinking") + 1] == "minimal"


def test_pi_default_blank_or_unknown_effort_refuses_only_thinking_cell() -> None:
    for effort in (None, "unsupported"):
        with pytest.raises(ValueError, match="no supported resolved effort"):
            normalize_pi_primary_session_args(
                ("--thinking", "high"), "subprocess", controls=_controls(effort=effort)
            )
    with pytest.raises(ValueError, match="no owned resolved model"):
        normalize_pi_primary_session_args(
            ("--model", "raw/model"), "subprocess", controls=_controls(model="", controlled=True)
        )


def test_pi_uncontrolled_model_cell_refuses_but_benign_arguments_survive() -> None:
    benign = ("--api-key", "secret-value", "--append-system-prompt", "keep this")
    normalized = normalize_pi_primary_session_args(
        benign, "subprocess", controls=_controls(model="raw/model", controlled=False)
    )
    assert normalized.remaining_args == benign
    assert normalized.warnings == ()

    with pytest.raises(ValueError, match="no owned resolved model"):
        normalize_pi_primary_session_args(
            (*benign, "--model", "raw/model"),
            "subprocess",
            controls=_controls(model="raw/model", controlled=False),
        )


def test_pi_missing_values_are_redacted_and_syntax_only_is_unchanged() -> None:
    for args in (("--model",), ("--thinking",), ("--api-key",)):
        with pytest.raises(ValueError) as error:
            normalize_pi_primary_session_args(args, "subprocess", controls=_controls())
        assert "secret-value" not in str(error.value)

    rejected_secret = ("--api-key", "SYNTHETIC_SECRET", "--unknown")
    with pytest.raises(ValueError) as error:
        normalize_pi_primary_session_args(rejected_secret, "subprocess", controls=_controls())
    assert "SYNTHETIC_SECRET" not in str(error.value)

    suppressed_secret = ("--model", "SYNTHETIC_SECRET")
    normalized_secret = normalize_pi_primary_session_args(
        suppressed_secret, "subprocess", controls=_controls()
    )
    assert normalized_secret.remaining_args == ()
    assert all("SYNTHETIC_SECRET" not in warning for warning in normalized_secret.warnings)

    original = ("--model", "raw/model", "--thinking", "high")
    normalized = normalize_pi_primary_session_args(original, "subprocess")
    assert normalized.remaining_args == original
    assert normalized.warnings == ()


def test_pi_managed_consumer_refuses_scalar_but_keeps_benign_api_key() -> None:
    benign = ("--api-key", "secret-value")
    assert normalize_pi_primary_session_args(
        benign, "managed", controls=_controls()
    ).remaining_args == benign
    with pytest.raises(ValueError, match="unsupported on this consumer"):
        normalize_pi_primary_session_args(
            ("--thinking", "high"), "managed", controls=_controls()
        )
