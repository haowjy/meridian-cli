from meridian.lib.harness.idle_types import CompactCosts
from meridian.lib.idle.worth import compact_worth


def test_compact_worth_table() -> None:
    costs = CompactCosts(
        w=2.0,
        c=0.1,
        u=20_000,
        o=5.0,
        summary_tokens=4_000,
        residue_tokens=13_000,
    )

    observed = compact_worth(
        stretch=7,
        context_tokens=52_300,
        baseline_tokens=52_300,
        baseline_source="observed",
        costs=costs,
    )
    compacted = compact_worth(
        stretch=7,
        context_tokens=52_300,
        baseline_tokens=52_300,
        baseline_source="compacted",
        costs=costs,
    )
    assert (observed.need_tokens, compacted.need_tokens) == (64_701, 50_256)

    below = compact_worth(
        stretch=7,
        context_tokens=52_300 + 50_255,
        baseline_tokens=52_300,
        baseline_source="compacted",
        costs=costs,
    )
    above = compact_worth(
        stretch=7,
        context_tokens=52_300 + 50_256,
        baseline_tokens=52_300,
        baseline_source="compacted",
        costs=costs,
    )
    assert (below.verdict, above.verdict) == ("not-worth-it", "worth-it")

    never_costs = costs.model_copy(update={"c": 1.0, "w": 1.0})
    never = compact_worth(
        stretch=7,
        context_tokens=200_000,
        baseline_tokens=50_000,
        baseline_source="observed",
        costs=never_costs,
    )
    unknown_costs = compact_worth(
        stretch=7,
        context_tokens=200_000,
        baseline_tokens=50_000,
        baseline_source="observed",
        costs=None,
    )
    overridden = compact_worth(
        stretch=7,
        context_tokens=50_001,
        baseline_tokens=50_000,
        baseline_source="observed",
        costs=never_costs,
        override_tokens=1,
    )
    assert (never.basis, never.verdict, never.need_tokens) == (
        "never",
        "not-worth-it",
        None,
    )
    assert unknown_costs.basis == "never"
    assert (overridden.basis, overridden.verdict) == ("configured", "worth-it")

    context_unknown = compact_worth(
        stretch=7,
        context_tokens=None,
        baseline_tokens=50_000,
        baseline_source="observed",
        costs=costs,
    )
    baseline_unknown = compact_worth(
        stretch=7,
        context_tokens=80_000,
        baseline_tokens=None,
        baseline_source="observed",
        costs=costs,
    )
    assert (context_unknown.verdict, baseline_unknown.verdict) == (
        "context-unknown",
        "baseline-unknown",
    )
