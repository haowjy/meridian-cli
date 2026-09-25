"""Pi harness extractor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.harness.attempt_facts import HarnessFailure
from meridian.lib.harness.common import coerce_optional_int
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.pi_failure import compact_pi_failure_output, pi_failure_from_payload
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import AttemptFold, HarnessExtractor


def _usage_from_message(message: Mapping[str, object]) -> TokenUsage | None:
    usage_obj = message.get("usage")
    if message.get("role") != "assistant" or not isinstance(usage_obj, dict):
        return None
    usage = cast("dict[str, object]", usage_obj)
    cost = usage.get("cost")
    total = cast("dict[str, object]", cost).get("total") if isinstance(cost, dict) else None
    return TokenUsage(
        input_tokens=coerce_optional_int(usage.get("input")),
        output_tokens=coerce_optional_int(usage.get("output")),
        cache_read_input_tokens=coerce_optional_int(usage.get("cacheRead")),
        cache_creation_input_tokens=coerce_optional_int(usage.get("cacheWrite")),
        total_cost_usd=float(total) if isinstance(total, int | float) else None,
    )


def _assistant_message_text(message: Mapping[str, object]) -> str | None:
    if str(message.get("role", "")).strip().lower() != "assistant":
        return None

    content_obj = message.get("content")
    if not isinstance(content_obj, list):
        return None

    texts: list[str] = []
    for part_obj in cast("list[object]", content_obj):
        if not isinstance(part_obj, dict):
            continue
        part = cast("dict[str, object]", part_obj)
        if str(part.get("type", "")).strip().lower() != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            texts.append(text)

    if not texts:
        return None
    return "\n".join(texts)


class PiHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for Pi artifacts and events."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        if event.event_type != "session":
            return None
        session_id = event.payload.get("id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
        return None

    def create_fold(self) -> AttemptFold:
        return PiFold(self)


class PiFold(AttemptFold):
    generic_usage = False

    def fold_event(self, kind: str, payload: Mapping[str, object]) -> None:
        facts = self.facts
        failure = pi_failure_from_payload(dict(payload))
        if failure:
            facts.failure = HarnessFailure(compact_pi_failure_output(failure), "pi_failure")
        if kind == "message_end":
            message = payload.get("message")
            if isinstance(message, dict):
                message = cast("dict[str, object]", message)
                usage = _usage_from_message(message)
                if usage is not None:
                    facts.usage = usage
                text = _assistant_message_text(message)
                if text:
                    self.set_text(text, "pi_message_end")
        elif kind == "agent_end":
            messages = payload.get("messages")
            if isinstance(messages, list):
                for message in reversed(cast("list[object]", messages)):
                    if isinstance(message, dict) and (
                        text := _assistant_message_text(cast("dict[str, object]", message))
                    ):
                        self.set_text(text, "pi_agent_end")
                        break


PI_EXTRACTOR = PiHarnessExtractor()

__all__ = [
    "PI_EXTRACTOR",
    "PiHarnessExtractor",
]
