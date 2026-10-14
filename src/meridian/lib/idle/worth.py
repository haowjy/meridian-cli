"""Pure growth break-even evaluation for idle compaction."""

from __future__ import annotations

import math

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


def compact_worth(
    *,
    stretch: int,
    context_tokens: int | None,
    baseline_tokens: int | None,
    baseline_source: BaselineSource,
    costs: CompactCosts | None,
    r: float = RETURN_PROBABILITY,
    override_tokens: int | None = None,
) -> CompactEval:
    """Evaluate whether observed growth covers the cost of compacting now."""

    if override_tokens is None and (costs is None or r * costs.w <= costs.c):
        return CompactEval(
            stretch=stretch,
            context_tokens=context_tokens,
            baseline_tokens=baseline_tokens,
            baseline_source=baseline_source,
            growth_tokens=(
                context_tokens - baseline_tokens
                if context_tokens is not None and baseline_tokens is not None
                else None
            ),
            need_tokens=None,
            basis="never",
            r=r,
            costs=costs,
            verdict="not-worth-it",
        )

    if override_tokens is not None:
        basis = "configured"
        need_tokens = override_tokens
    else:
        if costs is None:  # pragma: no cover - handled by the never branch
            raise AssertionError("break-even evaluation requires costs")
        basis = "break-even"
        delta = 0 if baseline_source == "compacted" else costs.residue_tokens
        if baseline_tokens is None:
            need_tokens = None
        else:
            need_tokens = math.ceil(
                (
                    r * costs.w * delta
                    + costs.c * baseline_tokens
                    + costs.u
                    + costs.o * costs.summary_tokens
                )
                / (r * costs.w - costs.c)
            )

    if context_tokens is None:
        verdict = "context-unknown"
        growth_tokens = None
    elif baseline_tokens is None:
        verdict = "baseline-unknown"
        growth_tokens = None
    else:
        growth_tokens = context_tokens - baseline_tokens
        if need_tokens is None:  # pragma: no cover - baseline checked above
            raise AssertionError("known baseline requires a compact threshold")
        verdict = "worth-it" if growth_tokens >= need_tokens else "not-worth-it"

    return CompactEval(
        stretch=stretch,
        context_tokens=context_tokens,
        baseline_tokens=baseline_tokens,
        baseline_source=baseline_source,
        growth_tokens=growth_tokens,
        need_tokens=need_tokens,
        basis=basis,
        r=r,
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
        return "need never (break-even)"
    growth = evaluation.growth_tokens
    if growth is None:  # pragma: no cover - unknown verdicts returned above
        return f"growth unknown, need {_tokens_k(evaluation.need_tokens)} ({evaluation.basis})"
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
