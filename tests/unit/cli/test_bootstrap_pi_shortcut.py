"""CLI bootstrap harness shortcut tests for Pi."""

from __future__ import annotations

from meridian.cli.bootstrap import HARNESS_SHORTCUT_NAMES, extract_global_options


def _normalize_output_format(value: str | None, json_mode: bool) -> str:
    if value:
        return value
    return "json" if json_mode else "text"


def test_pi_shortcut_is_registered() -> None:
    assert "pi" in HARNESS_SHORTCUT_NAMES


def test_extract_global_options_parses_pi_shortcut() -> None:
    cleaned, options = extract_global_options(
        ["pi", "spawn", "do work"],
        normalize_output_format=_normalize_output_format,
    )

    assert cleaned == ["spawn", "do work"]
    assert options.harness == "pi"
