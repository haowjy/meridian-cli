"""OpenCode harness extractor."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.native_identity import NativeKey
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.common import coerce_optional_float, coerce_optional_int
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.opencode_report import extract_opencode_session_id
from meridian.lib.harness.opencode_transcript import read_opencode_v2_turn
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import HarnessExtractor, fold_usage_fallback


class OpenCodeHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for OpenCode artifacts and events."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        return extract_opencode_session_id(dict(event.payload))

    def fold(self, facts: AttemptFacts, event: Mapping[str, object]) -> None:
        payload = dict(event)
        kind = str(payload.get("event_type", payload.get("type", "")))
        facts.output_seen = facts.output_seen or not kind.startswith("meridian.")
        session_id = extract_opencode_session_id(payload)
        if facts.scope_session_id and session_id != facts.scope_session_id:
            return
        if session_id and session_id.startswith("ses_"):
            facts.observe(session_id)
        if facts.first_session_id and session_id != facts.first_session_id:
            return
        fold_usage_fallback(facts, payload)
        if kind in {"session.idle", "message.updated"}:
            usage = _try_parse_opencode_usage(payload)
            if usage is not None:
                facts.usage_is_specific = True
                facts.usage = usage
        if kind == "session.text.ended":
            message_id = payload.get("assistantMessageID")
            if isinstance(message_id, str) and message_id:
                facts.native_turn_ids = (message_id,)
            text = payload.get("text")
            if isinstance(text, str) and text:
                facts.set_text(text, "opencode_v2_text")
            return
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            return
        properties = cast("dict[str, object]", properties)
        info = properties.get("info")
        if (
            kind == "message.updated"
            and isinstance(info, dict)
            and cast("dict[str, object]", info).get("role") == "assistant"
        ):
            info = cast("dict[str, object]", info)
            message_id = info.get("id")
            if isinstance(message_id, str) and message_id != facts.message_id:
                facts.message_id = message_id
                facts.final_text = None
                facts.final_text_source = None
            parts = info.get("parts")
            if isinstance(parts, list):
                text = "".join(
                    str(cast("dict[str, object]", part).get("text", ""))
                    for part in cast("list[object]", parts)
                    if isinstance(part, dict)
                    and cast("dict[str, object]", part).get("type") == "text"
                )
                if text.strip() and facts.final_text_source != "opencode_v1_parts":
                    facts.set_text(text.strip(), "opencode_v1_embedded")
        part = properties.get("part")
        if (
            kind == "message.part.updated"
            and isinstance(part, dict)
            and cast("dict[str, object]", part).get("type") == "text"
            and facts.message_id
            and cast("dict[str, object]", part).get(
                "messageID", cast("dict[str, object]", part).get("message_id")
            )
            == facts.message_id
        ):
            text = cast("dict[str, object]", part).get("text")
            if isinstance(text, str) and text.strip():
                prior = facts.final_text if facts.final_text_source == "opencode_v1_parts" else None
                facts.set_text((prior or "") + text.strip(), "opencode_v1_parts")

    def read_native_turn(self, key: NativeKey, turn_ids: tuple[str, ...]) -> str | None:
        try:
            return read_opencode_v2_turn(key, turn_ids)
        except sqlite3.Error:
            # A missing, busy or corrupt native store cannot invalidate live evidence.
            return None


OPENCODE_EXTRACTOR = OpenCodeHarnessExtractor()

__all__ = ["OPENCODE_EXTRACTOR", "OpenCodeHarnessExtractor"]


def _try_parse_opencode_usage(payload: dict[str, object]) -> TokenUsage | None:
    properties_obj = payload.get("properties")
    properties = (
        cast("dict[str, object]", properties_obj) if isinstance(properties_obj, dict) else None
    )
    nested_info_obj = properties.get("info") if properties is not None else None
    nested_info = (
        cast("dict[str, object]", nested_info_obj) if isinstance(nested_info_obj, dict) else None
    )
    nested_tokens_obj = nested_info.get("tokens") if nested_info is not None else None
    nested_tokens = (
        cast("dict[str, object]", nested_tokens_obj)
        if isinstance(nested_tokens_obj, dict)
        else None
    )
    if nested_tokens is not None:
        nested_cache_obj = nested_tokens.get("cache")
        nested_cache = (
            cast("dict[str, object]", nested_cache_obj)
            if isinstance(nested_cache_obj, dict)
            else {}
        )
        nested_usage = TokenUsage(
            input_tokens=coerce_optional_int(nested_tokens.get("input")),
            output_tokens=coerce_optional_int(nested_tokens.get("output")),
            cache_read_input_tokens=coerce_optional_int(nested_cache.get("read")),
            cache_creation_input_tokens=coerce_optional_int(nested_cache.get("write")),
            reasoning_tokens=coerce_optional_int(nested_tokens.get("reasoning")),
            total_cost_usd=coerce_optional_float(
                nested_info.get("cost") if nested_info is not None else None
            ),
        )
        if any(
            value is not None
            for value in (
                nested_usage.input_tokens,
                nested_usage.output_tokens,
                nested_usage.cache_read_input_tokens,
                nested_usage.cache_creation_input_tokens,
                nested_usage.reasoning_tokens,
            )
        ):
            return nested_usage

    info_obj = payload.get("info")
    info = cast("dict[str, object]", info_obj) if isinstance(info_obj, dict) else payload
    usage_obj = payload.get("usage")
    usage_source = cast("dict[str, object]", usage_obj) if isinstance(usage_obj, dict) else info
    usage = usage_source
    cost_obj = payload.get("cost")
    cost_source = cast("dict[str, object]", cost_obj) if isinstance(cost_obj, dict) else payload
    legacy_usage = TokenUsage(
        input_tokens=coerce_optional_int(usage.get("input_tokens") or usage.get("input")),
        output_tokens=coerce_optional_int(usage.get("output_tokens") or usage.get("output")),
        cache_read_input_tokens=coerce_optional_int(
            usage.get("cache_read_input_tokens") or usage.get("cache_read")
        ),
        cache_creation_input_tokens=coerce_optional_int(
            usage.get("cache_creation_input_tokens") or usage.get("cache_write")
        ),
        reasoning_tokens=coerce_optional_int(
            usage.get("reasoning_tokens") or usage.get("reasoning")
        ),
        total_cost_usd=coerce_optional_float(cost_source.get("total_cost_usd")),
    )
    if any(
        value is not None
        for value in (
            legacy_usage.input_tokens,
            legacy_usage.output_tokens,
            legacy_usage.cache_read_input_tokens,
            legacy_usage.cache_creation_input_tokens,
            legacy_usage.reasoning_tokens,
        )
    ):
        return legacy_usage
    return None
