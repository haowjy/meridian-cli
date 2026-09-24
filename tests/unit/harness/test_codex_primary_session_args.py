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
                "-m",
                "o4-mini",
                "--search",
            ),
            NormalizedNativeSessionArgs(
                NativeSessionSelector("resume", "123e4567-e89b-12d3-a456-426614174000"),
                ("--model", "gpt-5", "-m", "o4-mini", "--search"),
            ),
        ),
        (
            "subprocess",
            ("-c", 'model="gpt-5"', "--sandbox=workspace-write", "--full-auto"),
            NormalizedNativeSessionArgs(
                None, ("-c", 'model="gpt-5"', "--sandbox=workspace-write", "--full-auto")
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
        (
            "subprocess",
            ("resume", "123e4567-e89b-12d3-a456-426614174000", "--config", "unknown=true"),
        ),
        (
            "subprocess",
            ("resume", "123e4567-e89b-12d3-a456-426614174000", "-c", "sandbox_mode={x=1}"),
        ),
        ("subprocess", ("--model",)),
        ("subprocess", ("-m=gpt-5",)),
        ("subprocess", ("--made-up", "value")),
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
