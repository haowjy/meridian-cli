"""Chat event normalizer registry."""

from __future__ import annotations

from collections.abc import Callable

from meridian.lib.chat.normalization.base import EventNormalizer
from meridian.lib.chat.normalization.claude import ClaudeNormalizer
from meridian.lib.chat.normalization.codex import CodexNormalizer
from meridian.lib.chat.normalization.opencode import OpenCodeNormalizer
from meridian.lib.core.types import HarnessId

NormalizerFactory = Callable[[str, str], EventNormalizer]

NORMALIZER_REGISTRY: dict[str, NormalizerFactory] = {
    HarnessId.CLAUDE.value: ClaudeNormalizer,
    HarnessId.CODEX.value: CodexNormalizer,
    HarnessId.OPENCODE.value: OpenCodeNormalizer,
}


def get_normalizer_factory(harness_id: str | HarnessId) -> NormalizerFactory:
    """Return the normalizer factory for a harness id."""

    key = harness_id.value if isinstance(harness_id, HarnessId) else harness_id
    try:
        return NORMALIZER_REGISTRY[key]
    except KeyError as exc:
        raise KeyError(f"No chat event normalizer registered for harness {key!r}") from exc


__all__ = ["NORMALIZER_REGISTRY", "NormalizerFactory", "get_normalizer_factory"]
