import pytest

from meridian.lib.harness.native_session_args import NativeSessionSurface
from meridian.lib.harness.pi_native_source import (
    normalize_pi_primary_session_args,
    reject_pi_native_source_options,
)


@pytest.mark.parametrize(
    "args",
    [
        ("--continue",),
        ("-c",),
        ("--resume", "ses_abc"),
        ("-r", "ses_abc"),
        ("--session-id=ses_abc",),
        ("--session-id", "ses_abc"),
        ("--session=ses_abc",),
        ("--session", "ses_abc"),
        ("--fork",),
        ("--session-dir", "/tmp/override"),
        ("--session-dir=/tmp/override",),
    ],
)
def test_pi_raw_native_selector_options_are_rejected(args: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="Pi native-session selectors"):
        reject_pi_native_source_options(args)


def test_pi_non_session_passthrough_is_allowed() -> None:
    reject_pi_native_source_options(("--verbose", "--model=gpt-5.5"))


@pytest.mark.parametrize("surface", ["subprocess", "managed"])
def test_pi_primary_normalizer_preserves_bounded_inert_options(
    surface: NativeSessionSurface,
) -> None:
    args = (
        "--api-key", "secret-value", "--model=gpt-5.5", "-m", "gpt-5.4",
        "--thinking", "high", "--append-system-prompt", "extra instructions",
    )
    normalized = normalize_pi_primary_session_args(args, surface)
    assert normalized.selector is None
    assert normalized.remaining_args == args


@pytest.mark.parametrize("surface", ["subprocess", "managed"])
@pytest.mark.parametrize(
    "args",
    [
        ("--resume", "ses_secret"), ("--continue",), ("--session-id=ses_abc",),
        ("--fork",), ("--profile", "unsafe"), ("--session-dir", "/tmp/store"),
        ("--settings", "unsafe.json"), ("--unknown",), ("--model",),
        ("--thinking", "--resume"), ("--", "--model", "x"),
    ],
)
def test_pi_primary_normalizer_refuses_unbounded_or_selection_args(
    args: tuple[str, ...], surface: NativeSessionSurface,
) -> None:
    with pytest.raises(ValueError, match="Pi") as error:
        normalize_pi_primary_session_args(args, surface)
    assert "ses_secret" not in str(error.value)


@pytest.mark.parametrize("surface", ["subprocess", "managed"])
@pytest.mark.parametrize(
    "args",
    [
        ("SYNTHETIC_SECRET_POSITIONAL",),
        ("@SYNTHETIC_SECRET_RESPONSE_FILE",),
        ("--SYNTHETIC_SECRET_FLAG=value",),
        ("SYNTHETIC_SECRET_TOKEN=value",),
        ("--model=-SYNTHETIC_SECRET_VALUE",),
        ("-m=SYNTHETIC_SECRET_VALUE",),
    ],
)
def test_pi_primary_argument_errors_do_not_disclose_rejected_tokens(
    args: tuple[str, ...], surface: NativeSessionSurface,
) -> None:
    with pytest.raises(ValueError) as error:
        normalize_pi_primary_session_args(args, surface)
    assert "SYNTHETIC_SECRET" not in str(error.value)


@pytest.mark.parametrize("surface", ["subprocess", "managed"])
def test_pi_primary_argument_error_keeps_known_option_and_missing_value(
    surface: NativeSessionSurface,
) -> None:
    with pytest.raises(ValueError) as error:
        normalize_pi_primary_session_args(("--model",), surface)
    assert "--model" in str(error.value)
    assert "requires an unambiguous value" in str(error.value)
