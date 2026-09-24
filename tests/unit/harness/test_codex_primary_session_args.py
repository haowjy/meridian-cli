"""Codex primary raw arguments have a bounded, transport-aware grammar."""

import pytest

from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.native_session_args import (
    NativeSessionSelector,
    NativeSessionSurface,
    NormalizedNativeSessionArgs,
    PrimaryArgControls,
)


@pytest.mark.parametrize(
    ("surface", "args", "expected"),
    [
        (
            "subprocess",
            (
                "resume",
                "123e4567-e89b-12d3-a456-426614174000",
                "--model",
                "gpt-5",
                "-c",
                "model_reasoning_effort=high",
            ),
            NormalizedNativeSessionArgs(
                NativeSessionSelector("resume", "123e4567-e89b-12d3-a456-426614174000"),
                ("--model", "gpt-5", "-c", "model_reasoning_effort=high"),
            ),
        ),
        (
            "subprocess",
            ("-m", "o4-mini", "--dangerously-bypass-approvals-and-sandbox"),
            NormalizedNativeSessionArgs(
                None, ("-m", "o4-mini", "--dangerously-bypass-approvals-and-sandbox")
            ),
        ),
        (
            "managed",
            ("resume", "123e4567-e89b-12d3-a456-426614174000", "--config", 'model="gpt-5"'),
            NormalizedNativeSessionArgs(
                NativeSessionSelector("resume", "123e4567-e89b-12d3-a456-426614174000"),
                ("--config", 'model="gpt-5"'),
            ),
        ),
    ],
)
def test_normalizes_supported_selector_and_preserves_remainder(
    surface: NativeSessionSurface,
    args: tuple[str, ...],
    expected: NormalizedNativeSessionArgs,
) -> None:
    assert CodexAdapter().normalize_primary_session_args(args, surface) == expected


def test_accepts_one_raw_bypass_flag_byte_for_byte() -> None:
    args = ("--dangerously-bypass-approvals-and-sandbox",)

    assert CodexAdapter().normalize_primary_session_args(args, "subprocess") == (
        NormalizedNativeSessionArgs(None, args)
    )


@pytest.mark.parametrize(
    "args",
    [
        (
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-approvals-and-sandbox",
        ),
        (
            "resume",
            "123e4567-e89b-12d3-a456-426614174000",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-approvals-and-sandbox",
        ),
    ],
)
def test_refuses_duplicate_raw_bypass_flag(args: tuple[str, ...]) -> None:
    with pytest.raises(ValueError) as error:
        CodexAdapter().normalize_primary_session_args(args, "subprocess")

    assert str(error.value) == "duplicate Codex raw bypass option"


@pytest.mark.parametrize(
    ("surface", "args"),
    [
        ("subprocess", ("resume",)),
        ("subprocess", ("resume", "--last")),
        ("subprocess", ("--last",)),
        ("subprocess", ("--all",)),
        ("subprocess", ("fork", "123e4567-e89b-12d3-a456-426614174000")),
        ("subprocess", ("exec", "resume", "123e4567-e89b-12d3-a456-426614174000")),
        ("subprocess", ("resume", "123e4567-e89b-12d3-a456-426614174000", "second")),
        ("subprocess", ("resume", "123e4567-e89b-12d3-a456-426614174000", "--", "--model")),
        ("subprocess", ("resume", "123e4567-e89b-12d3-a456-426614174000", "@args.txt")),
        ("subprocess", ("resume", "c123")),
        ("subprocess", ("--model", "--help")),
        ("subprocess", ("--model", "gpt-5", "-m", "o4-mini")),
        ("subprocess", ("--model=gpt-5", "--model", "o4-mini")),
        ("subprocess", ("--sandbox=workspace-write",)),
        ("subprocess", ("--ask-for-approval", "never")),
        ("subprocess", ("--search",)),
        ("subprocess", ("--full-auto",)),
        ("managed", ("--sandbox=workspace-write",)),
        ("managed", ("--ask-for-approval", "never")),
        ("managed", ("--search",)),
        ("managed", ("--full-auto",)),
        (
            "subprocess",
            ("resume", "123e4567-e89b-12d3-a456-426614174000", "--config", "unknown=true"),
        ),
        (
            "subprocess",
            ("resume", "123e4567-e89b-12d3-a456-426614174000", "-c", "sandbox_mode={x=1}"),
        ),
        (
            "subprocess",
            (
                "-c",
                "model_reasoning_effort=high",
                "--config",
                "model_reasoning_effort=low",
            ),
        ),
        ("subprocess", ("--model",)),
        ("subprocess", ("-m=gpt-5",)),
        ("subprocess", ("-m", "--model=gpt-5")),
        ("subprocess", ("--made-up", "value")),
        ("subprocess", ("--private=DO_NOT_ECHO",)),
        ("subprocess", ("-c", "model='gpt-5'")),
        ("subprocess", ("-c", 'model="gpt-5"\n')),
        ("subprocess", ("-c", 'model="gpt-5\\n"')),
        ("subprocess", ("-c", 'model=["gpt-5"]')),
        ("subprocess", ("-c", "model=gpt-5\n")),
        ("subprocess", ("-c", "model=gpt-5=secret")),
        ("managed", ("--search",)),
        ("managed", ("--model", "gpt-5")),
        ("managed", ("-c", "profile=default")),
    ],
)
def test_refuses_unsupported_or_ambiguous_input(
    surface: NativeSessionSurface, args: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        CodexAdapter().normalize_primary_session_args(args, surface)


def test_raw_option_diagnostics_never_echo_equal_values() -> None:
    with pytest.raises(ValueError) as error:
        CodexAdapter().normalize_primary_session_args(("--private=DO_NOT_ECHO",), "subprocess")

    assert str(error.value) == "unsupported Codex raw option '--private'"
    assert "DO_NOT_ECHO" not in str(error.value)


def _controls(
    *,
    model: str | None = "owned-model",
    model_controlled: bool = True,
    sandbox: str | None = None,
    approval: str | None = None,
) -> PrimaryArgControls:
    return PrimaryArgControls(
        model=model,
        model_controlled=model_controlled,
        execution_policy=ResolvedExecutionPolicy(sandbox=sandbox, approval=approval),
    )


@pytest.mark.parametrize(
    "args",
    [
        ("-c", "model=other-model"),
        ("--config", 'model="other-model"'),
        ("--config=model=other-model",),
        ("-c", "model_reasoning_effort=low"),
        ("--config=model_reasoning_effort=high",),
        ("--config", 'tools.web_search="true"'),
    ],
)
def test_managed_config_scalars_refuse_with_typed_alternative_and_redaction(
    args: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError) as error:
        CodexAdapter().normalize_primary_session_args(args, "managed", controls=_controls())

    assert "managed app-server" in str(error.value)
    assert "use " in str(error.value)
    assert "other-model" not in str(error.value)
    assert "model_reasoning_effort=low" not in str(error.value)


@pytest.mark.parametrize(
    ("key", "value", "policy", "alternative"),
    [
        ("sandbox_mode", "danger-full-access", _controls(sandbox="read-only"), "--sandbox"),
        ("approval_policy", "never", _controls(approval="confirm"), "--approval"),
    ],
)
def test_managed_config_permission_conflicts_precede_model_refusal(
    key: str,
    value: str,
    policy: PrimaryArgControls,
    alternative: str,
) -> None:
    with pytest.raises(ValueError) as error:
        CodexAdapter().normalize_primary_session_args(
            ("-c", "model=raw-model", "--config", f"{key}={value}"),
            "managed",
            controls=policy,
        )

    assert "conflicts with Meridian" in str(error.value)
    assert alternative in str(error.value)
    assert "raw-model" not in str(error.value)
    assert value not in str(error.value)


def test_managed_model_alias_collision_is_ambiguous() -> None:
    with pytest.raises(ValueError, match="ambiguous repeated Codex model scalar"):
        CodexAdapter().normalize_primary_session_args(
            ("--model", "direct-model", "-c", "model=config-model"),
            "managed",
            controls=_controls(),
        )


def test_managed_matching_sandbox_is_still_refused_without_emission_proof() -> None:
    with pytest.raises(ValueError) as error:
        CodexAdapter().normalize_primary_session_args(
            ("--config=sandbox_mode=read-only",),
            "managed",
            controls=_controls(sandbox="read-only"),
        )

    assert "conflicts with Meridian" not in str(error.value)
    assert "use Meridian --sandbox" in str(error.value)


@pytest.mark.parametrize("args", [("-c", "model=raw-model"), ("-c", "model_reasoning_effort=low")])
def test_managed_model_and_effort_config_refuse_without_owned_values(
    args: tuple[str, ...],
) -> None:
    controls = _controls(model=None, model_controlled=False)

    with pytest.raises(ValueError) as error:
        CodexAdapter().normalize_primary_session_args(args, "managed", controls=controls)

    assert "managed app-server" in str(error.value)
    assert "raw-model" not in str(error.value)
    assert "model_reasoning_effort=low" not in str(error.value)


def test_config_spelling_remains_syntax_only_without_controls() -> None:
    args = ("--config", 'model="gpt-5"')
    assert CodexAdapter().normalize_primary_session_args(args, "managed") == (
        NormalizedNativeSessionArgs(None, args)
    )


@pytest.mark.parametrize("args", [("-c",), ("--config=model"), ("-c", "unknown=true")])
def test_config_missing_or_unknown_still_refuses(args: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        CodexAdapter().normalize_primary_session_args(args, "managed", controls=_controls())


@pytest.mark.parametrize(
    ("surface", "option", "surface_name"),
    [
        ("subprocess", "--sandbox", "subprocess"),
        ("managed", "--sandbox", "managed app-server"),
    ],
)
def test_refused_policy_option_points_to_typed_alternative(
    surface: NativeSessionSurface, option: str, surface_name: str
) -> None:
    with pytest.raises(ValueError) as error:
        CodexAdapter().normalize_primary_session_args((f"{option}=SECRET",), surface)

    assert str(error.value) == (
        f"Codex raw --sandbox is unsupported on {surface_name}; "
        "use Meridian --sandbox before the raw-tail delimiter."
    )
    assert "SECRET" not in str(error.value)
