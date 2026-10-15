import pytest

from meridian.lib.harness.idle_types import BaselineSource, CompactCosts
from meridian.lib.idle.worth import compact_worth

COSTS = CompactCosts(w=2.0, c=0.1, u=20_000, o=5.0, summary_tokens=4_000, residue_tokens=13_000)
NEVER = COSTS.model_copy(update={"c": 1.0, "w": 1.0})


@pytest.mark.parametrize(
    ("ctx", "base", "source", "costs", "override", "expected"),
    [
        (52_300, 52_300, "observed", COSTS, None, ("break-even", 64_701, "not-worth-it")),
        (52_300, 52_300, "compacted", COSTS, None, ("break-even", 50_256, "not-worth-it")),
        (102_555, 52_300, "compacted", COSTS, None, ("break-even", 50_256, "not-worth-it")),
        (102_556, 52_300, "compacted", COSTS, None, ("break-even", 50_256, "worth-it")),
        (200_000, 50_000, "observed", NEVER, None, ("never", None, "not-worth-it")),
        (200_000, 50_000, "observed", None, None, ("never", None, "not-worth-it")),
        (50_001, 50_000, "observed", NEVER, 1, ("configured", 1, "worth-it")),
        (None, 50_000, "observed", COSTS, None, ("break-even", 64_445, "context-unknown")),
        (80_000, None, "observed", COSTS, None, ("break-even", None, "baseline-unknown")),
    ],
)
def test_compact_worth_table(
    ctx: int | None, base: int | None, source: BaselineSource,
    costs: CompactCosts | None, override: int | None,
    expected: tuple[str, int | None, str],
) -> None:
    result = compact_worth(
        context_tokens=ctx, baseline_tokens=base, baseline_source=source,
        costs=costs, override_tokens=override,
    )
    assert (result.basis, result.need_tokens, result.verdict) == expected
