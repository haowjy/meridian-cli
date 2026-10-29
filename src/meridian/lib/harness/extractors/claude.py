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


_USAGE_KEYS = {
    "input_tokens": ("inputTokens", "input_tokens"),
    "output_tokens": ("outputTokens", "output_tokens"),
    "cache_read_input_tokens": ("cacheReadInputTokens", "cache_read_input_tokens"),
    "cache_creation_input_tokens": ("cacheCreationInputTokens", "cache_creation_input_tokens"),
}


def _usage_values(rows: list[dict[str, object]]) -> dict[str, int]:
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


def _token_usage(values: dict[str, int], cost: float | None) -> TokenUsage:
    return TokenUsage(
        input_tokens=values.get("input_tokens"),
        output_tokens=values.get("output_tokens"),
        cache_read_input_tokens=values.get("cache_read_input_tokens"),
        cache_creation_input_tokens=values.get("cache_creation_input_tokens"),
        total_cost_usd=cost,
    )


def _price_weight(values: dict[str, int]) -> float:
    """Tokens weighed by Anthropic's price ratios (output 5x input, cache read 0.1x, write 1.25x).

    Only ratios of weights are used: it splits a session's cost between the launches in it.
    """
    return (
        values.get("input_tokens", 0)
        + 5 * values.get("output_tokens", 0)
        + 0.1 * values.get("cache_read_input_tokens", 0)
        + 1.25 * values.get("cache_creation_input_tokens", 0)
    )


class ClaudeFold(AttemptFold):
    usage_baseline: dict[str, int] | None = None
    cost_baseline = 0.0

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

        ``modelUsage`` and ``total_cost_usd`` are cumulative over the whole Claude session:
        across the turns of this launch, and across earlier launches of a resumed session.
        ``usage`` is this turn alone. So the session's usage before this launch is the
        first result's cumulative minus its own turn, and every later result reports the
        cumulative minus that baseline. With no ``modelUsage`` the per-turn ``usage`` adds up.
        """
        model_usage = payload.get("modelUsage")
        turn = payload.get("usage")
        turn_values = (
            _usage_values([cast("dict[str, object]", turn)]) if isinstance(turn, dict) else {}
        )
        cost = coerce_optional_float(payload.get("total_cost_usd"))
        rows = (
            [
                cast("dict[str, object]", row)
                for row in cast("dict[str, object]", model_usage).values()
                if isinstance(row, dict)
            ]
            if isinstance(model_usage, dict)
            else []
        )
        if not rows:
            if turn_values:
                previous = self.facts.usage if self.usage_is_specific else None
                self.usage_is_specific = True
                self.facts.usage = _token_usage(
                    {
                        field: value + (getattr(previous, field, None) or 0)
                        for field, value in turn_values.items()
                    },
                    cost,
                )
            elif cost is not None:
                self.usage_is_specific = True
                self.facts.usage = TokenUsage(total_cost_usd=cost)
            return
        cumulative = _usage_values(rows)
        if cost is None:
            costs = [coerce_optional_float(row.get("costUSD")) for row in rows]
            cost = (
                sum(c for c in costs if c is not None)
                if any(c is not None for c in costs)
                else None
            )
        if self.usage_baseline is None:
            self.usage_baseline = {
                field: max(0, value - turn_values.get(field, value))
                for field, value in cumulative.items()
            }
            before = _price_weight(self.usage_baseline)
            self.cost_baseline = (
                cost * before / _price_weight(cumulative) if cost and before else 0.0
            )
        self.usage_is_specific = True
        self.facts.usage = _token_usage(
            {
                field: value - self.usage_baseline.get(field, 0)
                for field, value in cumulative.items()
            },
            None if cost is None else cost - self.cost_baseline,
        )


CLAUDE_EXTRACTOR = ClaudeHarnessExtractor()

__all__ = ["CLAUDE_EXTRACTOR", "ClaudeFold", "ClaudeHarnessExtractor"]
