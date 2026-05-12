"""Token cost estimation from mars model catalog pricing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.core.domain import TokenUsage


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


def _pricing_value(model: dict[str, object], *keys: str) -> float | None:
    # Mars cache uses flat keys like cost_input, cost_output, cost_cache_read
    for key in keys:
        value = _float(model.get(key))
        if value is not None:
            return value
    # Fallback: nested cost/pricing objects
    candidates: list[dict[str, object]] = []
    pricing = model.get("pricing")
    if isinstance(pricing, dict):
        candidates.append(cast("dict[str, object]", pricing))
    cost = model.get("cost")
    if isinstance(cost, dict):
        candidates.append(cast("dict[str, object]", cost))
    for source in candidates:
        for key in keys:
            value = _float(source.get(key))
            if value is not None:
                return value
    return None


def _load_model(model_id: str, project_root: Path | None = None) -> dict[str, object] | None:
    root = resolve_project_root_resolution(project_root).project_root
    path = root / ".mars" / "models-cache.json"
    try:
        raw_obj: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw_obj, dict):
        return None
    raw = cast("dict[str, object]", raw_obj)
    models_obj = raw.get("models")
    if not isinstance(models_obj, list):
        return None
    for item_obj in cast("list[object]", models_obj):
        if not isinstance(item_obj, dict):
            continue
        item = cast("dict[str, object]", item_obj)
        if str(item.get("id", "")).strip() == model_id:
            return item
    return None


def estimate_usage_cost(
    *,
    model_id: str | None,
    usage: TokenUsage,
    project_root: Path | None = None,
) -> TokenUsage:
    """Return usage with estimated total cost when catalog pricing is available.

    Catalog prices are expected per million tokens. If cache-read pricing is absent,
    cached input falls back to 10% of normal input cost.
    """
    if usage.total_cost_usd is not None or not model_id:
        return usage
    model = _load_model(model_id, project_root)
    if model is None:
        return usage

    # Mars cache keys: cost_input, cost_output, cost_cache_read, cost_cache_write, cost_reasoning
    input_per_m = _pricing_value(model, "cost_input", "input_cost_per_million", "input")
    output_per_m = _pricing_value(model, "cost_output", "output_cost_per_million", "output")
    cache_read_per_m = _pricing_value(
        model, "cost_cache_read", "cache_read_input_cost_per_million", "cache_read"
    )
    cache_write_per_m = _pricing_value(
        model, "cost_cache_write", "cache_creation_input_cost_per_million", "cache_write"
    )
    reasoning_per_m = _pricing_value(
        model, "cost_reasoning", "reasoning_cost_per_million", "reasoning"
    )

    if input_per_m is None and output_per_m is None:
        return usage
    if cache_read_per_m is None and input_per_m is not None:
        cache_read_per_m = input_per_m * 0.10
    if cache_write_per_m is None:
        cache_write_per_m = input_per_m
    if reasoning_per_m is None and output_per_m is not None:
        reasoning_per_m = output_per_m

    total = 0.0
    if usage.input_tokens is not None and input_per_m is not None:
        total += usage.input_tokens * input_per_m / 1_000_000
    if usage.output_tokens is not None and output_per_m is not None:
        total += usage.output_tokens * output_per_m / 1_000_000
    if usage.cache_read_input_tokens is not None and cache_read_per_m is not None:
        total += usage.cache_read_input_tokens * cache_read_per_m / 1_000_000
    if usage.cache_creation_input_tokens is not None and cache_write_per_m is not None:
        total += usage.cache_creation_input_tokens * cache_write_per_m / 1_000_000
    if usage.reasoning_tokens is not None and reasoning_per_m is not None:
        total += usage.reasoning_tokens * reasoning_per_m / 1_000_000
    if total <= 0:
        return usage
    return usage.model_copy(update={"total_cost_usd": total, "cost_is_estimate": True})
