"""OpenCode backend version resolution tests."""

from __future__ import annotations

import pytest

from meridian.lib.config.harness_models import OpenCodeHarnessProfileConfig
from meridian.lib.harness.opencode_backend import (
    normalize_version_preference,
    parse_opencode_version,
    resolve_opencode_version,
    v1_session_running,
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
    assert resolve_opencode_version("v2", env={}, detected=None) == "v2"
    assert resolve_opencode_version("v1", env={}, detected="v2") == "v1"


def test_auto_uses_detected_version() -> None:
    assert resolve_opencode_version("auto", env={}, detected="v1") == "v1"
    assert resolve_opencode_version("auto", env={}, detected="v2") == "v2"


def test_config_version_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match=r"harness\.opencode\.version"):
        OpenCodeHarnessProfileConfig(version="v3")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # Absent and null both mean the server is not working on this session.
        ({}, False),
        ({"ses_1": None}, False),
        ({"ses_1": {"type": "idle"}}, False),
        ({"ses_1": {"type": "busy"}}, True),
        # A provider backoff is a turn waiting to continue, not an idle session.
        ({"ses_1": {"type": "retry", "attempt": 1, "message": "slow", "next": 1}}, True),
        ({"ses_2": {"type": "busy"}}, False),
    ],
)
def test_v1_session_running_reads_the_session_status(
    payload: object,
    expected: bool,
) -> None:
    assert v1_session_running(payload, "ses_1") is expected


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"ses_1": "busy"},
        {"ses_1": {}},
        {"ses_1": {"type": 1}},
        {"ses_1": {"type": "paused"}},
    ],
)
def test_v1_session_running_refuses_an_answer_it_cannot_read(payload: object) -> None:
    """An unreadable status is no answer, not "the turn ended"."""

    with pytest.raises(ValueError):
        v1_session_running(payload, "ses_1")
