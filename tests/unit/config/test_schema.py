"""Unit tests for config schema scalar codecs."""

from __future__ import annotations

import pytest

from meridian.lib.config.schema import parse_cli_scalar


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("[]", ()),
        ("claude,pi", ("claude", "pi")),
        ('["claude","pi"]', ("claude", "pi")),
        ("a,,b", ("a", "b")),
    ],
)
def test_parse_cli_scalar_str_list_accepts_valid_values(
    raw_value: str,
    expected: tuple[str, ...],
) -> None:
    result = parse_cli_scalar(
        canonical_key="spawn.deny_headless_harnesses",
        value_kind="str_list",
        raw_value=raw_value,
    )

    assert result == expected


@pytest.mark.parametrize(
    ("raw_value", "message"),
    [
        ('[""]', "expected non-empty items"),
        ('[" "]', "expected non-empty items"),
        ('["" , ""]', "expected non-empty items"),
        ("", "expected comma-separated values"),
    ],
)
def test_parse_cli_scalar_str_list_rejects_empty_values(raw_value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_cli_scalar(
            canonical_key="spawn.deny_headless_harnesses",
            value_kind="str_list",
            raw_value=raw_value,
        )
