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
    "raw_value",
    ['[""]', '[" "]', '["" , ""]'],
)
def test_parse_cli_scalar_str_list_rejects_all_empty_items(raw_value: str) -> None:
    with pytest.raises(ValueError, match="expected non-empty items"):
        parse_cli_scalar(
            canonical_key="spawn.deny_headless_harnesses",
            value_kind="str_list",
            raw_value=raw_value,
        )


def test_parse_cli_scalar_str_list_rejects_bare_empty_string() -> None:
    with pytest.raises(ValueError, match="expected comma-separated values"):
        parse_cli_scalar(
            canonical_key="spawn.deny_headless_harnesses",
            value_kind="str_list",
            raw_value="",
        )
