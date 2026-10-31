"""Claude and Codex report usage cumulative over a session; a launch's own is end minus seed.

Events are trimmed from real streams (Claude haiku/sonnet ``result``, Codex ``app-server``
``thread/tokenUsage/updated``): only the usage fields the fold reads are kept.
"""

from __future__ import annotations

from typing import Any

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.connections.base import RawHarnessEvent


def _fold(harness: HarnessId, events: list[dict[str, Any]], seed: Any = None) -> TokenUsage:
    fold = get_harness_bundle(harness).extractor.create_fold(seed)
    for payload in events:
        fold(
            RawHarnessEvent(
                harness_id=str(harness), event_type=str(payload["type"]), payload=payload
            )
        )
    assert fold.facts.usage is not None
    return fold.facts.usage


def _row(i: int, o: int, r: int, w: int, cost: float) -> dict[str, Any]:
    return {
        "inputTokens": i,
        "outputTokens": o,
        "cacheReadInputTokens": r,
        "cacheCreationInputTokens": w,
        "costUSD": cost,
    }


def test_claude_fresh_launch_with_a_subagent_is_the_whole_cumulative() -> None:
    # ``usage`` holds the main model's turn only; ``modelUsage`` also has the subagent's row.
    result = {
        "type": "result",
        "usage": {"input_tokens": 3, "output_tokens": 40},
        "total_cost_usd": 0.0036,
        "modelUsage": {
            "claude-haiku": _row(3, 40, 5000, 25000, 0.0030),
            "claude-sonnet": _row(4, 520, 13638, 14713, 0.0006),
        },
    }
    usage = _fold(HarnessId.CLAUDE, [result])
    assert (usage.input_tokens, usage.output_tokens) == (7, 560)
    assert (usage.cache_read_input_tokens, usage.cache_creation_input_tokens) == (18638, 39713)
    assert usage.total_cost_usd == 0.0036 and not usage.cost_is_estimate


def test_claude_resume_on_another_model_subtracts_per_model() -> None:
    first = _fold(
        HarnessId.CLAUDE,
        [
            {
                "type": "result",
                "total_cost_usd": 0.01,
                "modelUsage": {"claude-haiku": _row(10, 100, 1000, 2000, 0.01)},
            }
        ],
    )
    assert first.harness_cumulative is not None
    # The session continues on sonnet: haiku's row stays, sonnet's is new.
    second = _fold(
        HarnessId.CLAUDE,
        [
            {
                "type": "result",
                "total_cost_usd": 0.0634,
                "modelUsage": {
                    "claude-haiku": _row(10, 100, 1000, 2000, 0.01),
                    "claude-sonnet": _row(6, 627, 80991, 3576, 0.0534),
                },
            }
        ],
        seed=first.harness_cumulative,
    )
    assert (second.input_tokens, second.output_tokens) == (6, 627)
    assert (second.cache_read_input_tokens, second.cache_creation_input_tokens) == (80991, 3576)
    assert second.total_cost_usd is not None and abs(second.total_cost_usd - 0.0534) < 1e-9


def test_codex_resume_counts_only_the_launch_after_the_stored_total() -> None:
    def updated(total: int) -> dict[str, Any]:
        return {
            "type": "thread/tokenUsage/updated",
            "tokenUsage": {
                "total": {"inputTokens": total, "outputTokens": total // 10, "cachedInputTokens": 0}
            },
        }

    first = _fold(HarnessId.CODEX, [updated(1000), updated(2000)])
    assert first.input_tokens == 2000 and first.harness_cumulative is not None
    # The resume replays the earlier total, then the new turn grows it.
    second = _fold(HarnessId.CODEX, [updated(2000), updated(2500)], seed=first.harness_cumulative)
    assert (second.input_tokens, second.output_tokens) == (500, 50)
    assert second.harness_cumulative is not None
    assert second.harness_cumulative["inputTokens"] == 2500
