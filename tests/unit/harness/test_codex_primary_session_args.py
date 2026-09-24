"""Codex primary raw arguments have a bounded, transport-aware grammar."""

import pytest

from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.native_session_args import (
    NativeSessionSelector,
    NativeSessionSurface,
    NormalizedNativeSessionArgs,
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
            ("resume", "123e4567-e89b-12d3-a456-426614174000", "--config", "model=\"gpt-5\""),
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
        ("subprocess", ("-c", "model=\"gpt-5\"\n")),
        ("subprocess", ("-c", 'model="gpt-5\\n"')),
        ("subprocess", ("-c", "model=[\"gpt-5\"]")),
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
        CodexAdapter().normalize_primary_session_args(
            ("--private=DO_NOT_ECHO",), "subprocess"
        )

    assert str(error.value) == "unsupported Codex raw option '--private'"
    assert "DO_NOT_ECHO" not in str(error.value)


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
