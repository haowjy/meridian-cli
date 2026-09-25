"""Cursor harness extractor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.common import coerce_optional_int, extract_text, iter_nested_dicts
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import (
    HarnessExtractor,
    fold_usage_fallback,
    normalize_harness_event_type,
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


def _is_result_event(payload: Mapping[str, object]) -> bool:
    event_type = normalize_harness_event_type(payload)
    return event_type == "result" or event_type.endswith(".result")


def _find_session_id(payload: Mapping[str, object]) -> str | None:
    return session_from_mapping_with_keys(payload, _SESSION_ID_KEYS)


class CursorHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for Cursor artifacts and events."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        return _find_session_id(event.payload)

    def fold(self, facts: AttemptFacts, event: Mapping[str, object]) -> None:
        payload = dict(event)
        fold_usage_fallback(facts, payload)
        facts.output_seen = facts.output_seen or not normalize_harness_event_type(
            payload
        ).startswith("meridian.")
        facts.observe(_find_session_id(payload))
        if _is_result_event(payload):
            for nested in iter_nested_dicts(payload):
                usage = nested.get("usage")
                if isinstance(usage, dict):
                    usage = cast("dict[str, object]", usage)
                    facts.usage_is_specific = True
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
                        facts.set_text(text, "cursor_result")
        elif (
            normalize_harness_event_type(payload) == "assistant"
            and facts.final_text_source != "cursor_result"
        ):
            text = extract_text(payload.get("message"))
            if text:
                facts.set_text(text, "cursor_assistant")


CURSOR_EXTRACTOR = CursorHarnessExtractor()

__all__ = ["CURSOR_EXTRACTOR", "CursorHarnessExtractor"]
