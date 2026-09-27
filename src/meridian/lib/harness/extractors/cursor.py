"""Cursor harness extractor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.harness.common import coerce_optional_int, extract_text, iter_nested_dicts
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import (
    AttemptFold,
    HarnessExtractor,
    session_from_mapping_with_keys,
)

_SESSION_ID_KEYS: tuple[str, ...] = (
    "session_id",
    "sessionId",
    "sessionID",
    "session",
    "conversation_id",
    "conversationId",
)


def _find_session_id(payload: Mapping[str, object]) -> str | None:
    return session_from_mapping_with_keys(payload, _SESSION_ID_KEYS)


class CursorHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for Cursor artifacts and events."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        return _find_session_id(event.payload)

    def create_fold(self) -> AttemptFold:
        return CursorFold(self)


class CursorFold(AttemptFold):
    def fold_event(self, kind: str, payload: Mapping[str, object]) -> None:
        facts = self.facts
        if kind == "result" or kind.endswith(".result"):
            for nested in iter_nested_dicts(dict(payload)):
                usage = nested.get("usage")
                if isinstance(usage, dict):
                    usage = cast("dict[str, object]", usage)
                    self.usage_is_specific = True
                    facts.usage = TokenUsage(
                        input_tokens=coerce_optional_int(usage.get("inputTokens")),
                        output_tokens=coerce_optional_int(usage.get("outputTokens")),
                        cache_read_input_tokens=coerce_optional_int(usage.get("cacheReadTokens")),
                        cache_creation_input_tokens=coerce_optional_int(
                            usage.get("cacheWriteTokens")
                        ),
                    )
                for key in ("result", "text", "output", "content", "message"):
                    text = extract_text(nested.get(key))
                    if text:
                        self.set_text(text, "cursor_result")
        elif kind == "assistant" and self.text_source != "cursor_result":
            text = extract_text(payload.get("message"))
            if text:
                self.set_text(text, "cursor_assistant")


CURSOR_EXTRACTOR = CursorHarnessExtractor()

__all__ = ["CURSOR_EXTRACTOR", "CursorHarnessExtractor"]
