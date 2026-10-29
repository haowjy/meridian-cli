"""Claude harness extractor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.harness.common import coerce_optional_float, coerce_optional_int, extract_text
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.permission_broker import HEADLESS_REJECT_REASON
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state.permission_journal import PermissionDenial

from .base import AttemptFold, HarnessExtractor

if TYPE_CHECKING:
    from pydantic import JsonValue


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

    def create_fold(self, seed: Mapping[str, JsonValue] | None = None) -> AttemptFold:
        return ClaudeFold(self, seed=seed)


def _permission_denial_detail(tool: str, tool_input: object) -> str:
    if isinstance(tool_input, dict):
        values = cast("dict[str, object]", tool_input)
        for key in ("command", "file_path", "path"):
            value = values.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return tool


_USAGE_KEYS = {
    "input_tokens": ("inputTokens", "input_tokens"),
    "output_tokens": ("outputTokens", "output_tokens"),
    "cache_read_input_tokens": ("cacheReadInputTokens", "cache_read_input_tokens"),
    "cache_creation_input_tokens": ("cacheCreationInputTokens", "cache_creation_input_tokens"),
}


def _usage_values(rows: list[Mapping[str, object]]) -> dict[str, int]:
    """Each token field summed over ``rows``; fields no row reports are left out."""
    values: dict[str, int] = {}
    for field, keys in _USAGE_KEYS.items():
        known = [
            n
            for row in rows
            if (n := coerce_optional_int(row.get(keys[0], row.get(keys[1])))) is not None
        ]
        if known:
            values[field] = sum(known)
    return values


def _token_usage(
    values: dict[str, int], cost: float | None, cumulative: dict[str, JsonValue] | None = None
) -> TokenUsage:
    return TokenUsage(
        input_tokens=values.get("input_tokens"),
        output_tokens=values.get("output_tokens"),
        cache_read_input_tokens=values.get("cache_read_input_tokens"),
        cache_creation_input_tokens=values.get("cache_creation_input_tokens"),
        total_cost_usd=cost,
        harness_cumulative=cumulative,
    )


def _dict_rows(raw: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): cast("Mapping[str, object]", row)
        for name, row in cast("dict[object, object]", raw).items()
        if isinstance(row, dict)
    }


class ClaudeFold(AttemptFold):
    def fold_event(self, kind: str, payload: Mapping[str, object]) -> None:
        facts = self.facts
        if kind == "result":
            text = extract_text(payload.get("result"))
            if text:
                self.set_text(text, "claude_result")
            self._fold_result_usage(payload)
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

    def _fold_result_usage(self, payload: Mapping[str, object]) -> None:
        """Fold one ``result`` event's usage into this launch's facts.

        ``modelUsage`` (a row per model, subagents included) and ``total_cost_usd`` are
        cumulative over the whole Claude session, earlier launches of a resumed session
        too. This launch's usage is that cumulative minus the seed (the previous launch's
        stored cumulative; empty for a fresh launch), per model, so tokens and cost are
        exact. The cumulative itself is kept for the next launch. A ``result`` without
        ``modelUsage`` carries no usage this fold uses.
        """
        rows = _dict_rows(payload.get("modelUsage"))
        cost = coerce_optional_float(payload.get("total_cost_usd"))
        if not rows:
            return
        if cost is None:
            costs = [coerce_optional_float(row.get("costUSD")) for row in rows.values()]
            known_costs = [c for c in costs if c is not None]
            cost = sum(known_costs) if known_costs else None
        seed_rows = _dict_rows(self.seed.get("modelUsage") if self.seed else None)
        seed_cost = coerce_optional_float(self.seed.get("cost") if self.seed else None) or 0.0
        own: dict[str, int] = {}
        for model, row in rows.items():
            seed_values = _usage_values([seed_rows[model]]) if model in seed_rows else {}
            for field, value in _usage_values([row]).items():
                own[field] = own.get(field, 0) + max(0, value - seed_values.get(field, 0))
        self.usage_is_specific = True
        self.facts.usage = _token_usage(
            own,
            None if cost is None else max(0.0, cost - seed_cost),
            {
                "modelUsage": cast("dict[str, JsonValue]", dict(rows)),
                "cost": cost,
            },
        )


CLAUDE_EXTRACTOR = ClaudeHarnessExtractor()

__all__ = ["CLAUDE_EXTRACTOR", "ClaudeFold", "ClaudeHarnessExtractor"]
