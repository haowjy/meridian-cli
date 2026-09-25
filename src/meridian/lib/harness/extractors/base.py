"""Typed harness extractor protocol."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Generic, Protocol, TypeVar, cast, runtime_checkable

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.native_identity import NativeKey
from meridian.lib.harness.adapter import SpawnExtractor
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.common import (
    coerce_optional_float,
    coerce_optional_int,
    iter_nested_dicts,
)
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

ExtractorSpecT = TypeVar("ExtractorSpecT", bound=ResolvedLaunchSpec, covariant=True)


@runtime_checkable
class HarnessExtractor(SpawnExtractor, Protocol, Generic[ExtractorSpecT]):
    """Harness-owned extraction surface shared by subprocess and streaming."""

    def read_native_turn(self, key: NativeKey, turn_ids: tuple[str, ...]) -> str | None:
        return None

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        """Best-effort extraction from one live event frame."""
        ...


def session_from_mapping_with_keys(
    payload: Mapping[str, object],
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nested in payload.values():
        if isinstance(nested, dict):
            found = session_from_mapping_with_keys(cast("dict[str, object]", nested), keys)
            if found:
                return found
    return None


def normalize_harness_event_type(
    payload: Mapping[str, object],
    *,
    keys: tuple[str, ...] = ("event_type", "event", "type"),
) -> str:
    """Normalize event type fields to a stable dot-separated lowercase value."""

    raw_type: object = ""
    for key in keys:
        if key in payload:
            raw_type = payload.get(key, "")
            break
    return str(raw_type).strip().lower().replace("/", ".")


__all__ = [
    "HarnessExtractor",
    "normalize_harness_event_type",
    "session_from_mapping_with_keys",
]


def fold_usage_fallback(facts: AttemptFacts, event: Mapping[str, object]) -> None:
    """Keep the first best generic live usage until a harness-specific total arrives."""
    if facts.usage_is_specific:
        return
    usage = facts.usage or TokenUsage()
    for payload in iter_nested_dicts(dict(event)):
        candidate = TokenUsage()
        for input_key, output_key in (
            ("input_tokens", "output_tokens"),
            ("input", "output"),
            ("prompt_tokens", "completion_tokens"),
            ("prompt_token_count", "completion_token_count"),
            ("inputTokenCount", "outputTokenCount"),
        ):
            if input_key in payload or output_key in payload:
                candidate = TokenUsage(
                    input_tokens=coerce_optional_int(payload.get(input_key)),
                    output_tokens=coerce_optional_int(payload.get(output_key)),
                )
                break
        if sum(v is not None for v in (candidate.input_tokens, candidate.output_tokens)) > sum(
            v is not None for v in (usage.input_tokens, usage.output_tokens)
        ):
            usage = usage.model_copy(
                update={
                    "input_tokens": candidate.input_tokens,
                    "output_tokens": candidate.output_tokens,
                }
            )
        if usage.total_cost_usd is None:
            for cost_key in ("total_cost_usd", "cost_usd", "cost", "total_cost", "totalCostUsd"):
                cost = coerce_optional_float(payload.get(cost_key))
                if cost is not None:
                    usage = usage.model_copy(update={"total_cost_usd": cost})
                    break
    if usage != TokenUsage():
        facts.usage = usage
