"""OpenCode's bounded raw primary-session argument grammar."""

import pytest

from meridian.lib.harness.opencode import normalize_primary_session_args


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (("--session", "ses_native", "--model", "openai/gpt", "--print-logs"),
         ("ses_native", ("--model", "openai/gpt", "--print-logs"))),
        (("--session=ses_native", "-m", "openai/gpt"),
         ("ses_native", ("-m", "openai/gpt"))),
        (("-s", "ses_native", "--agent", "coder", "--variant=high"),
         ("ses_native", ("--agent", "coder", "--variant=high"))),
    ],
)
def test_subprocess_normalizes_explicit_session_and_preserves_remainder(raw, expected) -> None:
    result = normalize_primary_session_args(raw)
    assert result.selector is not None
    assert (result.selector.native_id, result.remaining_args) == expected


def test_selector_like_text_in_an_option_value_is_not_a_selector() -> None:
    result = normalize_primary_session_args(("--model=--session", "--session", "ses_native"))
    assert result.selector is not None
    assert result.selector.native_id == "ses_native"
    assert result.remaining_args == ("--model=--session",)


@pytest.mark.parametrize(
    ("raw", "surface", "message"),
    [
        (("--continue",), "subprocess", "implicit"),
        (("-c",), "subprocess", "implicit"),
        (("--session", "one", "-s", "two"), "subprocess", "duplicate"),
        (("--session",), "subprocess", "missing"),
        (("--session", "--model"), "subprocess", "value"),
        (("--fork",), "subprocess", "typed"),
        (("attach", "http://localhost"), "subprocess", "positional"),
        (("--", "prompt"), "subprocess", "--"),
        (("@args.txt",), "subprocess", "@file"),
        (("--server", "http://localhost"), "subprocess", "endpoint"),
        (("--mystery", "x"), "subprocess", "unknown"),
        (("--log-level",), "subprocess", "missing"),
        (("--log-level", "TRACE"), "subprocess", "log-level"),
        (("--model", "openai/gpt"), "managed", "typed model"),
        (("-m", "openai/gpt"), "managed", "typed model"),
        (("--agent", "coder"), "managed", "typed agent"),
        (("--variant", "high"), "managed", "typed variant"),
    ],
)
def test_unsupported_forms_refuse(raw, surface, message) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_primary_session_args(raw, surface=surface)


def test_managed_accepts_only_global_logging_flags_and_session_selector() -> None:
    result = normalize_primary_session_args(
        ("--session=ses_native", "--print-logs", "--log-level", "WARN"),
        surface="managed",
    )
    assert result.selector is not None
    assert result.selector.native_id == "ses_native"
    assert result.remaining_args == ("--print-logs", "--log-level", "WARN")


def test_normalization_without_selector_preserves_supported_subprocess_options() -> None:
    raw = ("--model", "openai/gpt", "--agent", "coder", "--variant", "high", "--print-logs")
    result = normalize_primary_session_args(raw)
    assert result.selector is None
    assert result.remaining_args == raw
