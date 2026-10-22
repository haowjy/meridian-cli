"""Claude harness extractor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.harness.common import coerce_optional_float, coerce_optional_int, extract_text
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.permission_broker import HEADLESS_REJECT_REASON
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state.permission_journal import PermissionDenial

from .base import AttemptFold, HarnessExtractor


class ClaudeHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for Claude artifacts and events."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        if event.event_type not in {"system", "result", "assistant", "user"}:
            return None
        for key in ("session_id", "sessionId", "sessionID"):
            value = event.payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def create_fold(self) -> AttemptFold:
        return ClaudeFold(self)


def _permission_denial_detail(tool: str, tool_input: object) -> str:
    if isinstance(tool_input, dict):
        values = cast("dict[str, object]", tool_input)
        for key in ("command", "file_path", "path"):
            value = values.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return tool


class ClaudeFold(AttemptFold):
    def fold_event(self, kind: str, payload: Mapping[str, object]) -> None:
        facts = self.facts
        if kind == "result":
            text = extract_text(payload.get("result"))
            if text:
                self.set_text(text, "claude_result")
            model_usage = payload.get("modelUsage")
            usage: object = (
                cast("dict[str, object]", model_usage)
                if isinstance(model_usage, dict)
                else payload.get("usage")
            )
            if isinstance(usage, dict):
                usage = cast("dict[str, object]", usage)
                rows = [cast("dict[str, object]", x) for x in usage.values() if isinstance(x, dict)]
                if not rows:
                    rows = [usage]
                fields = {
                    "input_tokens": ("inputTokens", "input_tokens"),
                    "output_tokens": ("outputTokens", "output_tokens"),
                    "cache_read_input_tokens": ("cacheReadInputTokens", "cache_read_input_tokens"),
                    "cache_creation_input_tokens": (
                        "cacheCreationInputTokens",
                        "cache_creation_input_tokens",
                    ),
                }
                values: dict[str, int | None] = {}
                for field, keys in fields.items():
                    numbers = [
                        coerce_optional_int(row.get(keys[0], row.get(keys[1]))) for row in rows
                    ]
                    known = [n for n in numbers if n is not None]
                    previous = getattr(facts.usage, field, None) if self.usage_is_specific else None
                    values[field] = (sum(known) + (previous or 0)) if known else previous
                cost = coerce_optional_float(payload.get("total_cost_usd"))
                if cost is None:
                    costs = [coerce_optional_float(row.get("costUSD")) for row in rows]
                    known_costs = [c for c in costs if c is not None]
                    cost = (
                        sum(known_costs)
                        if known_costs
                        else (facts.usage.total_cost_usd if facts.usage else None)
                    )
                self.usage_is_specific = True
                facts.usage = TokenUsage(
                    input_tokens=values["input_tokens"],
                    output_tokens=values["output_tokens"],
                    cache_read_input_tokens=values["cache_read_input_tokens"],
                    cache_creation_input_tokens=values["cache_creation_input_tokens"],
                    total_cost_usd=cost,
                )
            elif (cost := coerce_optional_float(payload.get("total_cost_usd"))) is not None:
                self.usage_is_specific = True
                facts.usage = TokenUsage(total_cost_usd=cost)
            raw_denials = payload.get("permission_denials")
            if isinstance(raw_denials, list):
                recorded = {denial.request_id for denial in facts.permission_denials}
                for raw_denial in cast("list[object]", raw_denials):
                    if not isinstance(raw_denial, dict):
                        continue
                    denial = cast("dict[str, object]", raw_denial)
                    request_id = denial.get("tool_use_id")
                    tool = denial.get("tool_name")
                    if (
                        not isinstance(request_id, str)
                        or not request_id.strip()
                        or not isinstance(tool, str)
                        or not tool.strip()
                        or request_id in recorded
                    ):
                        continue
                    facts.permission_denials.append(
                        PermissionDenial(
                            request_id=request_id,
                            tool=tool,
                            detail=_permission_denial_detail(tool, denial.get("tool_input")),
                            reason=HEADLESS_REJECT_REASON,
                        )
                    )
                    recorded.add(request_id)
        elif kind == "assistant" and self.text_source != "claude_result":
            text = extract_text(payload.get("content")) or extract_text(payload.get("message"))
            if text:
                self.set_text(text, "claude_assistant")


CLAUDE_EXTRACTOR = ClaudeHarnessExtractor()

__all__ = ["CLAUDE_EXTRACTOR", "ClaudeFold", "ClaudeHarnessExtractor"]
