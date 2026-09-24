import pytest

from meridian.lib.harness.pi_native_source import reject_pi_native_source_options


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
