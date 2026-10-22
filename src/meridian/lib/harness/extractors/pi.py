"""Pi harness extractor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.harness.attempt_facts import HarnessFailure
from meridian.lib.harness.common import coerce_optional_float, coerce_optional_int
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.permission_broker import HEADLESS_REJECT_REASON
from meridian.lib.harness.pi_failure import compact_pi_failure_output, pi_failure_from_payload
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state.permission_journal import PermissionDenial

from .base import AttemptFold, HarnessExtractor


def _usage_from_message(message: Mapping[str, object]) -> TokenUsage | None:
    usage_obj = message.get("usage")
    if message.get("role") != "assistant":
        return None
    if not isinstance(usage_obj, dict):
        return TokenUsage()
    usage = cast("dict[str, object]", usage_obj)
    cost = usage.get("cost")
    total = cast("dict[str, object]", cost).get("total") if isinstance(cost, dict) else None
    return TokenUsage(
        input_tokens=coerce_optional_int(usage.get("input")),
        output_tokens=coerce_optional_int(usage.get("output")),
        cache_read_input_tokens=coerce_optional_int(usage.get("cacheRead")),
        cache_creation_input_tokens=coerce_optional_int(usage.get("cacheWrite")),
        total_cost_usd=coerce_optional_float(total),
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


def _add_usage(prior: TokenUsage | None, usage: TokenUsage) -> TokenUsage:
    """Message usage is incremental; a missing operand leaves its total unknown."""
    if prior is None:
        return usage

    def total(left: int | None, right: int | None) -> int | None:
        return left + right if left is not None and right is not None else None

    return TokenUsage(
        input_tokens=total(prior.input_tokens, usage.input_tokens),
        output_tokens=total(prior.output_tokens, usage.output_tokens),
        cache_read_input_tokens=total(prior.cache_read_input_tokens, usage.cache_read_input_tokens),
        cache_creation_input_tokens=total(
            prior.cache_creation_input_tokens, usage.cache_creation_input_tokens
        ),
        total_cost_usd=(
            coerce_optional_float(prior.total_cost_usd + usage.total_cost_usd)
            if prior.total_cost_usd is not None and usage.total_cost_usd is not None
            else None
        ),
    )


def _permission_denial(payload: Mapping[str, object]) -> PermissionDenial | None:
    """Read the policy error the Pi permission extension returns for a blocked call."""

    request_id = payload.get("toolCallId")
    tool = payload.get("toolName")
    result = payload.get("result")
    if (
        payload.get("isError") is not True
        or not isinstance(request_id, str)
        or not isinstance(tool, str)
        or not isinstance(result, dict)
    ):
        return None
    content = cast("dict[str, object]", result).get("content")
    if not isinstance(content, list):
        return None
    text: str | None = None
    for item in cast("list[object]", content):
        if not isinstance(item, dict):
            continue
        part = cast("dict[str, object]", item)
        text_value = part.get("text")
        if part.get("type") == "text" and isinstance(text_value, str):
            text = text_value
            break
    if text is None:
        return None
    normalized = " ".join(text.split())
    if normalized == HEADLESS_REJECT_REASON:
        detail, reason = tool, normalized
    elif normalized.startswith("Blocked by Meridian policy: "):
        reason, marker, detail = normalized.rpartition(" Attempted: ")
        if not marker:
            reason, detail = normalized, tool
    else:
        return None
    return PermissionDenial(
        request_id=request_id,
        tool=tool,
        detail=" ".join(detail.split())[:1024],
        reason=" ".join(reason.split())[:1024],
    )


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
                    self.usage_is_specific = True
                    facts.usage = _add_usage(facts.usage, usage)
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
        elif kind == "tool_execution_end" and (denial := _permission_denial(payload)):
            facts.permission_denials.append(denial)


PI_EXTRACTOR = PiHarnessExtractor()

__all__ = [
    "PI_EXTRACTOR",
    "PiHarnessExtractor",
]
