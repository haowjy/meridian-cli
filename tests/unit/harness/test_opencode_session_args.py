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
        (("--session", "ses_one", "-s", "ses_two"), "subprocess", "duplicate"),
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


@pytest.mark.parametrize("surface", ["subprocess", "managed"])
@pytest.mark.parametrize(
    "native_id",
    ["c123", "p123", "latest", "ses_", "ses_title/path", "ses_title with spaces"],
)
def test_session_selector_requires_native_opencode_id(surface, native_id) -> None:
    with pytest.raises(ValueError, match="exact native session ID"):
        normalize_primary_session_args(("--session", native_id), surface=surface)


@pytest.mark.parametrize("surface", ["subprocess", "managed"])
@pytest.mark.parametrize("args", [("-s=ses_native",), ("-m=openai/gpt",)])
def test_short_option_inline_values_refuse(surface, args) -> None:
    with pytest.raises(ValueError):
        normalize_primary_session_args(args, surface=surface)


@pytest.mark.parametrize("surface", ["subprocess", "managed"])
@pytest.mark.parametrize("level", ["debug", "warn", "Trace", "ERROR "])
def test_log_level_requires_exact_uppercase_enum(surface, level) -> None:
    with pytest.raises(ValueError, match="DEBUG, INFO, WARN, or ERROR"):
        normalize_primary_session_args(("--log-level", level), surface=surface)


@pytest.mark.parametrize("surface", ["subprocess", "managed"])
@pytest.mark.parametrize(
    "args",
    [
        ("PRIVATE_POSITIONAL_SENTINEL",),
        ("@PRIVATE_RESPONSE_PATH_SENTINEL",),
        ("--PRIVATE_UNKNOWN_SENTINEL",),
    ],
)
def test_refusal_diagnostics_do_not_echo_arbitrary_tokens(surface, args) -> None:
    with pytest.raises(ValueError) as error:
        normalize_primary_session_args(args, surface=surface)
    for secret in (
        "PRIVATE_POSITIONAL_SENTINEL",
        "PRIVATE_RESPONSE_PATH_SENTINEL",
        "PRIVATE_UNKNOWN_SENTINEL",
    ):
        assert secret not in str(error.value)


@pytest.mark.parametrize("surface", ["subprocess", "managed"])
@pytest.mark.parametrize(
    "args",
    [
        ("--profile", "PRIVATE_PROFILE_SENTINEL"),
        ("-p", "PRIVATE_PROFILE_SENTINEL"),
        ("--profile=PRIVATE_PROFILE_SENTINEL",),
    ],
)
def test_profile_refusal_names_typed_alternative_without_value(surface, args) -> None:
    with pytest.raises(ValueError, match="typed resolved settings") as error:
        normalize_primary_session_args(args, surface=surface)
    assert "PRIVATE_PROFILE_SENTINEL" not in str(error.value)


@pytest.mark.parametrize("surface", ["subprocess", "managed"])
def test_native_session_selector_and_option_remainders_are_exact(surface) -> None:
    args = ("-s", "ses_A-09_Z", "--log-level", "WARN")
    result = normalize_primary_session_args(args, surface=surface)
    assert result.selector is not None
    assert result.selector.native_id == "ses_A-09_Z"
    assert result.remaining_args == ("--log-level", "WARN")
