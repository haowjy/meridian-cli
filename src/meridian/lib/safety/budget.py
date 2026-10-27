"""Budget configuration and incremental cost tracking."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.domain import TokenUsage


class Budget(BaseModel):
    """Budget limits in USD."""

    model_config = ConfigDict(frozen=True)

    per_run_usd: float | None = None
    per_space_usd: float | None = None


class BudgetBreach(BaseModel):
    """Observed budget breach metadata."""

    model_config = ConfigDict(frozen=True)

    scope: Literal["run", "space"]
    observed_usd: float
    limit_usd: float


class LiveBudgetTracker(BaseModel):
    """Streaming budget tracker fed by harness stdout events."""

    model_config = ConfigDict()

    budget: Budget
    space_spent_usd: float = 0.0
    run_cost_usd: float = 0.0

    def observe_usage(self, usage: TokenUsage | None) -> BudgetBreach | None:
        """Consume a harness fold's cumulative cost, independent of wire shape."""
        if usage is None or usage.total_cost_usd is None:
            return self.check()
        return self.observe_cost(usage.total_cost_usd)

    def observe_cost(self, cost_usd: float) -> BudgetBreach | None:
        """Update the current run cost and return breach details when exceeded."""

        if cost_usd < 0:
            return None
        if cost_usd > self.run_cost_usd:
            self.run_cost_usd = cost_usd
        return self.check()

    def check(self) -> BudgetBreach | None:
        """Evaluate per-run and per-space limits."""

        per_run = self.budget.per_run_usd
        if per_run is not None and self.run_cost_usd > per_run:
            return BudgetBreach(scope="run", observed_usd=self.run_cost_usd, limit_usd=per_run)

        per_space = self.budget.per_space_usd
        if per_space is not None:
            observed_space = self.space_spent_usd + self.run_cost_usd
            if observed_space > per_space:
                return BudgetBreach(
                    scope="space",
                    observed_usd=observed_space,
                    limit_usd=per_space,
                )
        return None
