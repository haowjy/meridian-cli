import json
import math
from pathlib import Path

from meridian.lib.core.domain import TokenUsage
from meridian.lib.harness.cost import estimate_usage_cost


def _write_models_cache(project_root: Path, *models: dict[str, object]) -> None:
    mars_dir = project_root / ".mars"
    mars_dir.mkdir()
    (mars_dir / "models-cache.json").write_text(
        json.dumps({"models": list(models)}),
        encoding="utf-8",
    )


def test_codex_openai_estimate_treats_cached_and_reasoning_tokens_as_inclusive(
    tmp_path: Path,
) -> None:
    _write_models_cache(
        tmp_path,
        {
            "id": "gpt-5.5",
            "provider": "OpenAI",
            "cost_input": 5.0,
            "cost_cache_read": 0.5,
            "cost_output": 30.0,
        },
    )

    usage = estimate_usage_cost(
        model_id="gpt-5.5",
        usage=TokenUsage(
            input_tokens=10_196_095,
            cache_read_input_tokens=9_899_648,
            output_tokens=19_031,
            reasoning_tokens=2_086,
        ),
        project_root=tmp_path,
        harness_id="codex",
    )

    assert usage.cost_is_estimate is True
    assert usage.total_cost_usd is not None
    assert math.isclose(usage.total_cost_usd, 7.002989)


def test_opencode_estimate_preserves_additive_token_buckets(tmp_path: Path) -> None:
    _write_models_cache(
        tmp_path,
        {
            "id": "gpt-5.5",
            "provider": "OpenAI",
            "cost_input": 5.0,
            "cost_cache_read": 0.5,
            "cost_cache_write": 6.0,
            "cost_output": 30.0,
            "cost_reasoning": 30.0,
        },
    )

    usage = estimate_usage_cost(
        model_id="gpt-5.5",
        usage=TokenUsage(
            input_tokens=1_000_000,
            cache_read_input_tokens=500_000,
            cache_creation_input_tokens=250_000,
            output_tokens=100_000,
            reasoning_tokens=50_000,
        ),
        project_root=tmp_path,
        harness_id="opencode",
    )

    expected = 5.0 + 0.25 + 1.5 + 3.0 + 1.5
    assert usage.cost_is_estimate is True
    assert usage.total_cost_usd is not None
    assert math.isclose(usage.total_cost_usd, expected)


def test_harness_provided_total_cost_takes_precedence(tmp_path: Path) -> None:
    _write_models_cache(
        tmp_path,
        {
            "id": "gpt-5.5",
            "provider": "OpenAI",
            "cost_input": 5.0,
            "cost_output": 30.0,
        },
    )
    direct_usage = TokenUsage(
        input_tokens=10_000_000,
        output_tokens=1_000_000,
        total_cost_usd=1.23,
    )

    usage = estimate_usage_cost(
        model_id="gpt-5.5",
        usage=direct_usage,
        project_root=tmp_path,
        harness_id="codex",
    )

    assert usage is direct_usage
    assert usage.total_cost_usd == 1.23
    assert usage.cost_is_estimate is False
