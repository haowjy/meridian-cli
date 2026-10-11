from __future__ import annotations

from collections.abc import Mapping

import pytest

from meridian.lib.harness import pi_idle
from meridian.lib.harness.idle_types import CacheTtl


@pytest.mark.parametrize(
    ("provider", "env", "expected"),
    [
        ("anthropic", {}, CacheTtl(300, estimated=False)),
        ("anthropic", {"PI_CACHE_RETENTION": "short"}, CacheTtl(300, estimated=False)),
        ("Anthropic", {"PI_CACHE_RETENTION": " LONG "}, CacheTtl(3600, estimated=False)),
        ("openai", {}, None),
        ("openai", {"PI_CACHE_RETENTION": "short"}, None),
        ("OpenAI", {"PI_CACHE_RETENTION": "long"}, CacheTtl(1800, estimated=True)),
        ("google", {"PI_CACHE_RETENTION": "long"}, None),
        (None, {"PI_CACHE_RETENTION": "long"}, None),
    ],
)
def test_pi_detect_ttl_provider_retention_matrix(
    provider: str | None,
    env: Mapping[str, str],
    expected: CacheTtl | None,
) -> None:
    assert pi_idle.detect_ttl(provider, env) == expected
