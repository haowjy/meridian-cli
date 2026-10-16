"""OpenCode backend version resolution tests."""

from __future__ import annotations

import pytest

from meridian.lib.config.harness_models import OpenCodeHarnessProfileConfig
from meridian.lib.harness.opencode_backend import (
    normalize_version_preference,
    parse_opencode_version,
    resolve_opencode_version,
)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("1.18.31\n", "v1"),
        ("1.0.0", "v1"),
        ("opencode v2.0.6\n", "v2"),
        ("2.0.6", "v2"),
        ("opencode v10.1.0", "v2"),
        ("", None),
        ("not a version", None),
    ],
)
def test_parse_opencode_version(output: str, expected: str | None) -> None:
    assert parse_opencode_version(output) == expected


def test_normalize_version_preference() -> None:
    assert normalize_version_preference(" V2 ") == "v2"
    assert normalize_version_preference("v1") == "v1"
    assert normalize_version_preference("auto") == "auto"
    assert normalize_version_preference(None) == "auto"
    assert normalize_version_preference("nonsense") == "auto"


@pytest.mark.parametrize(
    ("preference", "detected", "expected"),
    [
        ("v2", None, "v2"),
        ("v1", "v2", "v1"),
        ("auto", "v1", "v1"),
        ("auto", "v2", "v2"),
    ],
)
def test_resolve_opencode_version(
    preference: str,
    detected: str | None,
    expected: str,
) -> None:
    assert resolve_opencode_version(preference, env={}, detected=detected) == expected


def test_config_version_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match=r"harness\.opencode\.version"):
        OpenCodeHarnessProfileConfig(version="v3")
