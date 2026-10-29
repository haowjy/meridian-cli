"""Codex harness extractor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.harness.common import coerce_optional_int, extract_codex_thread_id, extract_text
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import AttemptFold, HarnessExtractor

if TYPE_CHECKING:
    from pydantic import JsonValue


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

    def create_fold(self, seed: Mapping[str, JsonValue] | None = None) -> AttemptFold:
        return CodexFold(self, seed=seed)


class CodexFold(AttemptFold):
    main_thread_id: str | None = None

    def accepts(self, kind: str, event: RawHarnessEvent) -> bool:
        thread_id = extract_codex_thread_id(event.payload)
        owners = (self.scope_session_id, self.main_thread_id)
        return not thread_id or all(not owner or owner == thread_id for owner in owners)

    def fold_event(self, kind: str, payload: Mapping[str, object]) -> None:
        facts = self.facts
        if kind == "turn.started" and self.main_thread_id is None:
            self.main_thread_id = extract_codex_thread_id(dict(payload))
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
        if kind == "thread.tokenusage.updated":
            token_usage = payload.get("tokenUsage") or _nested_get(payload, "payload", "tokenUsage")
            if isinstance(token_usage, dict):
                self._fold_thread_usage(cast("dict[str, object]", token_usage))
        elif kind == "turn.completed":
            usage = payload.get("usage") or _nested_get(payload, "payload", "usage")
            if isinstance(usage, dict):
                self.usage_is_specific = True
                facts.usage = _token_usage(cast("dict[str, object]", usage))

    def _fold_thread_usage(self, token_usage: dict[str, object]) -> None:
        """Codex reports the thread's running ``total``, earlier launches of a resumed thread
        included. This launch's usage is that total minus the seed (the previous launch's
        stored total; empty for a fresh launch); the total is kept for the next launch.
        """
        total = token_usage.get("total")
        if not isinstance(total, dict):
            return
        total = cast("dict[str, object]", total)
        self.usage_is_specific = True
        self.facts.usage = _minus(
            _token_usage(total),
            _token_usage(dict(self.seed)) if self.seed else TokenUsage(),
        ).model_copy(update={"harness_cumulative": cast("dict[str, JsonValue]", total)})


def _minus(usage: TokenUsage, before: TokenUsage) -> TokenUsage:
    def diff(now: int | None, then: int | None) -> int | None:
        return None if now is None else max(0, now - (then or 0))

    return TokenUsage(
        input_tokens=diff(usage.input_tokens, before.input_tokens),
        output_tokens=diff(usage.output_tokens, before.output_tokens),
        cache_read_input_tokens=diff(usage.cache_read_input_tokens, before.cache_read_input_tokens),
        cache_creation_input_tokens=diff(
            usage.cache_creation_input_tokens, before.cache_creation_input_tokens
        ),
        reasoning_tokens=diff(usage.reasoning_tokens, before.reasoning_tokens),
    )


def _token_usage(usage: dict[str, object]) -> TokenUsage:
    return TokenUsage(
        input_tokens=coerce_optional_int(usage.get("inputTokens", usage.get("input_tokens"))),
        output_tokens=coerce_optional_int(usage.get("outputTokens", usage.get("output_tokens"))),
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
