"""OpenCode backend version resolution tests."""

from __future__ import annotations

import pytest

from meridian.lib.config.settings import OpenCodeHarnessProfileConfig
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


def test_explicit_preference_wins_without_probe() -> None:
    assert resolve_opencode_version("v2", detected=None) == "v2"
    assert resolve_opencode_version("v1", detected="v2") == "v1"


def test_auto_uses_detected_version() -> None:
    assert resolve_opencode_version("auto", detected="v1") == "v1"
    assert resolve_opencode_version("auto", detected="v2") == "v2"


def test_adapter_allows_named_primary_resume() -> None:
    from meridian.lib.harness.opencode import OpenCodeAdapter

    assert OpenCodeAdapter().capabilities.supports_named_primary_resume is True


def test_adapter_does_not_advertise_session_fork() -> None:
    from meridian.lib.harness.opencode import OpenCodeAdapter

    assert OpenCodeAdapter().capabilities.supports_session_fork is False


def test_config_version_defaults_to_auto() -> None:
    assert OpenCodeHarnessProfileConfig().version == "auto"
    assert OpenCodeHarnessProfileConfig(version="v2").version == "v2"


def test_config_version_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match=r"harness\.opencode\.version"):
        OpenCodeHarnessProfileConfig(version="v3")
