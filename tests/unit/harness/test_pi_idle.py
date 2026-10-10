from __future__ import annotations

from collections.abc import Mapping

import pytest

from meridian.lib.harness import pi_idle


@pytest.mark.parametrize(
    ("provider", "env", "expected"),
    [
        ("anthropic", {}, 300),
        ("anthropic", {"PI_CACHE_RETENTION": "short"}, 300),
        ("Anthropic", {"PI_CACHE_RETENTION": " LONG "}, 3600),
        ("openai", {}, None),
        ("openai", {"PI_CACHE_RETENTION": "short"}, None),
        ("OpenAI", {"PI_CACHE_RETENTION": "long"}, 1800),
        ("google", {"PI_CACHE_RETENTION": "long"}, None),
        (None, {"PI_CACHE_RETENTION": "long"}, None),
    ],
)
def test_pi_detect_ttl_provider_retention_matrix(
    provider: str | None,
    env: Mapping[str, str],
    expected: int | None,
) -> None:
    assert pi_idle.detect_ttl(provider, env) == expected
