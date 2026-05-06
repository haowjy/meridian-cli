import pytest

from meridian.lib.chat.normalization.base import EventNormalizer
from meridian.lib.chat.normalization.claude import ClaudeNormalizer
from meridian.lib.chat.normalization.codex import CodexNormalizer
from meridian.lib.chat.normalization.opencode import OpenCodeNormalizer
from meridian.lib.chat.normalization.registry import NORMALIZER_REGISTRY, get_normalizer_factory
from meridian.lib.harness.ids import HarnessId


@pytest.mark.parametrize(
    ("harness_id", "expected_factory"),
    [
        (HarnessId.CLAUDE, ClaudeNormalizer),
        (HarnessId.CODEX, CodexNormalizer),
        (HarnessId.OPENCODE, OpenCodeNormalizer),
    ],
)
def test_registry_returns_factories_for_supported_harnesses(
    harness_id: HarnessId,
    expected_factory: type[EventNormalizer],
) -> None:
    factory = get_normalizer_factory(harness_id)

    assert NORMALIZER_REGISTRY[harness_id.value] is expected_factory
    assert factory is expected_factory

    normalizer = factory("chat-1", "exec-1")
    assert hasattr(normalizer, "normalize")
    assert hasattr(normalizer, "reset")


def test_registry_raises_descriptive_error_for_unknown_harness():
    with pytest.raises(KeyError, match="No chat event normalizer registered for harness 'unknown'"):
        get_normalizer_factory("unknown")
