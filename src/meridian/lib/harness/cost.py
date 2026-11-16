from __future__ import annotations

from typing import Literal

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import HarnessId

CostSemantics = Literal["additive", "openai_inclusive"]


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip().lstrip("$"))
        except ValueError:
            return None
    return None


def _cost_semantics(model: dict[str, object], harness_id: str | None) -> CostSemantics:
    """Return token bucket semantics for catalog-based estimation.

    OpenAI/Codex reports expose cache-read and reasoning as breakdowns of inclusive
    input/output totals. OpenCode artifacts observed in the wild expose additive
    buckets, even when the underlying model provider is OpenAI.
    """

    normalized_harness = (harness_id or "").strip().lower()
    if normalized_harness == HarnessId.OPENCODE.value:
        return "additive"
    if normalized_harness == HarnessId.CODEX.value:
        return "openai_inclusive"

    provider = str(model.get("provider_key", "")).strip().lower()
    if provider == "openai":
        return "openai_inclusive"
    return "additive"


def estimate_usage_cost(
    *,
    catalog_row: dict[str, object] | None,
    usage: TokenUsage,
    harness_id: str | None = None,
) -> TokenUsage:
    """Return usage with estimated total cost from a Mars ``routing.catalog_row``.

    Catalog prices are per million tokens. If cache-read pricing is absent,
    cached input falls back to 10% of normal input cost.
    """
    if usage.total_cost_usd is not None or catalog_row is None:
        return usage
    model = catalog_row

    input_per_m = _float(model.get("cost_input"))
    output_per_m = _float(model.get("cost_output"))
    cache_read_per_m = _float(model.get("cost_cache_read"))
    cache_write_per_m = _float(model.get("cost_cache_write"))
    reasoning_per_m = _float(model.get("cost_reasoning"))

    if input_per_m is None and output_per_m is None:
        return usage
    if cache_read_per_m is None and input_per_m is not None:
        cache_read_per_m = input_per_m * 0.10
    if cache_write_per_m is None:
        cache_write_per_m = input_per_m
    if reasoning_per_m is None and output_per_m is not None:
        reasoning_per_m = output_per_m

    semantics = _cost_semantics(model, harness_id)
    total = 0.0
    if semantics == "openai_inclusive":
        cached_input = usage.cache_read_input_tokens or 0
        cache_created = usage.cache_creation_input_tokens or 0
        if usage.input_tokens is not None and input_per_m is not None:
            uncached_input = max(usage.input_tokens - cached_input - cache_created, 0)
            total += uncached_input * input_per_m / 1_000_000
        if usage.cache_read_input_tokens is not None and cache_read_per_m is not None:
            total += usage.cache_read_input_tokens * cache_read_per_m / 1_000_000
        if usage.output_tokens is not None and output_per_m is not None:
            total += usage.output_tokens * output_per_m / 1_000_000
        elif usage.reasoning_tokens is not None and reasoning_per_m is not None:
            total += usage.reasoning_tokens * reasoning_per_m / 1_000_000
    else:
        if usage.input_tokens is not None and input_per_m is not None:
            total += usage.input_tokens * input_per_m / 1_000_000
        if usage.output_tokens is not None and output_per_m is not None:
            total += usage.output_tokens * output_per_m / 1_000_000
        if usage.cache_read_input_tokens is not None and cache_read_per_m is not None:
            total += usage.cache_read_input_tokens * cache_read_per_m / 1_000_000
        if usage.reasoning_tokens is not None and reasoning_per_m is not None:
            total += usage.reasoning_tokens * reasoning_per_m / 1_000_000
    if usage.cache_creation_input_tokens is not None and cache_write_per_m is not None:
        total += usage.cache_creation_input_tokens * cache_write_per_m / 1_000_000
    if total <= 0:
        return usage
    return usage.model_copy(update={"total_cost_usd": total, "cost_is_estimate": True})
