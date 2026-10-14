"""Pure growth break-even evaluation for idle compaction."""

from __future__ import annotations

import math
from typing import Literal, cast

from meridian.lib.harness.idle_types import (
    BaselineSource,
    CompactCosts,
    CompactEval,
)

# State files retain only the latest stretch, which biases the measured 0.27
# return rate down: each session's terminal, unreturned stretch dominates. The
# small sample conditioned on reaching warn/compact was 1/3. Revisit this first
# once per-stretch outcomes make the conditional rate measurable.
RETURN_PROBABILITY = 0.5


def _threshold(
    baseline: int | None,
    source: BaselineSource,
    costs: CompactCosts | None,
    override: int | None,
) -> tuple[Literal["break-even", "configured", "never"], int | None]:
    if override is not None:
        return "configured", override
    if costs is None or RETURN_PROBABILITY * costs.w <= costs.c:
        return "never", None
    if baseline is None:
        return "break-even", None
    delta = 0 if source == "compacted" else costs.residue_tokens
    paid = (
        RETURN_PROBABILITY * costs.w * delta
        + costs.c * baseline + costs.u + costs.o * costs.summary_tokens
    )
    # Growth is integral and the inequality strict: exact break-even needs one more token.
    return "break-even", math.floor(paid / (RETURN_PROBABILITY * costs.w - costs.c)) + 1


def compact_worth(
    *,
    context_tokens: int | None,
    baseline_tokens: int | None,
    baseline_source: BaselineSource,
    costs: CompactCosts | None,
    override_tokens: int | None = None,
) -> CompactEval:
    """Evaluate whether observed growth covers the cost of compacting now."""

    basis, need = _threshold(baseline_tokens, baseline_source, costs, override_tokens)
    growth = (
        None if context_tokens is None or baseline_tokens is None
        else context_tokens - baseline_tokens
    )
    if basis == "never":
        verdict = "not-worth-it"
    elif context_tokens is None:
        verdict = "context-unknown"
    elif growth is None or need is None:
        verdict = "baseline-unknown"
    else:
        verdict = "worth-it" if growth >= need else "not-worth-it"
    return CompactEval(
        context_tokens=context_tokens,
        baseline_tokens=baseline_tokens,
        baseline_source=baseline_source,
        growth_tokens=growth,
        need_tokens=need,
        basis=basis,
        r=RETURN_PROBABILITY,
        costs=costs,
        verdict=verdict,
    )


def compact_eval_detail(evaluation: CompactEval) -> str:
    """Render one compact evaluation for the CLI fire wire."""

    if evaluation.verdict == "context-unknown":
        return "context unknown"
    if evaluation.verdict == "baseline-unknown":
        return "baseline unknown"
    if evaluation.need_tokens is None:
        return "need never"
    growth = cast("int", evaluation.growth_tokens)  # Known for numeric-threshold verdicts.
    operator = ">=" if evaluation.verdict == "worth-it" else "<"
    return (
        f"growth {_tokens_k(growth)} {operator} need {_tokens_k(evaluation.need_tokens)} "
        f"({evaluation.basis})"
    )


def _tokens_k(tokens: int) -> str:
    return f"{round(tokens / 1000)}k"


__all__ = [
    "RETURN_PROBABILITY",
    "compact_eval_detail",
    "compact_worth",
]
