"""Codex harness extractor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.harness.common import coerce_optional_int, extract_codex_thread_id, extract_text
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import AttemptFold, HarnessExtractor


def _owned_session_id(payload: Mapping[str, object], event_type: str) -> str | None:
    if event_type.replace("/", ".") not in {"thread.started", "session_id"}:
        return None
    for key in ("thread_id", "threadId", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    thread = payload.get("thread")
    if isinstance(thread, dict):
        value = cast("dict[str, object]", thread).get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class CodexHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for Codex artifacts and events."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        return _owned_session_id(event.payload, event.event_type)

    def create_fold(self) -> AttemptFold:
        return CodexFold(self)


class CodexFold(AttemptFold):
    main_thread_id: str | None = None

    def accepts(self, kind: str, event: RawHarnessEvent) -> bool:
        thread_id = extract_codex_thread_id(event.payload)
        if self.scope_session_id and thread_id and thread_id != self.scope_session_id:
            return False
        if kind == "turn.started" and self.main_thread_id is None:
            self.main_thread_id = thread_id
        return not (self.main_thread_id and thread_id and self.main_thread_id != thread_id)

    def fold_event(self, kind: str, payload: Mapping[str, object]) -> None:
        facts = self.facts
        item = payload.get("item")
        if isinstance(item, dict):
            item = cast("dict[str, object]", item)
            item_type = str(item.get("type", "")).lower().replace("_", "")
            if kind == "item.completed" and item_type == "agentmessage":
                text = extract_text(item.get("text"))
                if text:
                    self.set_text(text, "codex_agent_message")
            elif kind == "item.started" and item_type == "commandexecution":
                facts.final_text = None
                self.text_source = None
        usage: object = None
        if kind == "thread.tokenusage.updated":
            token_usage = payload.get("tokenUsage") or _nested_get(payload, "payload", "tokenUsage")
            if isinstance(token_usage, dict):
                usage = cast("dict[str, object]", token_usage).get("total")
        elif kind == "turn.completed":
            usage = payload.get("usage") or _nested_get(payload, "payload", "usage")
        if isinstance(usage, dict):
            usage = cast("dict[str, object]", usage)
            self.usage_is_specific = True
            facts.usage = TokenUsage(
                input_tokens=coerce_optional_int(
                    usage.get("inputTokens", usage.get("input_tokens"))
                ),
                output_tokens=coerce_optional_int(
                    usage.get("outputTokens", usage.get("output_tokens"))
                ),
                cache_read_input_tokens=coerce_optional_int(
                    usage.get("cachedInputTokens", usage.get("cached_input_tokens"))
                ),
                cache_creation_input_tokens=coerce_optional_int(
                    usage.get("cacheCreationInputTokens", usage.get("cache_creation_input_tokens"))
                ),
                reasoning_tokens=coerce_optional_int(
                    usage.get("reasoningOutputTokens", usage.get("reasoning_output_tokens"))
                ),
            )


CODEX_EXTRACTOR = CodexHarnessExtractor()

__all__ = ["CODEX_EXTRACTOR", "CodexHarnessExtractor"]


def _nested_get(payload: object, *keys: str) -> object:
    current: object = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = cast("Mapping[str, object]", current).get(key)
    return current
