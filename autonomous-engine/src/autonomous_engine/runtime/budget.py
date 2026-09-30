"""Budget manager: runtime, tokens, cost, parallelism (plan.md §26, §58).

Time is a runtime budget, not the definition of success. The budget decides
when to *stop*; the completion criteria decide whether the project is *done*.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..core.config import BudgetConfig
from ..core.task import now_iso


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class BudgetManager:
    config: BudgetConfig
    started_at: float = field(default_factory=time.time)
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    model_calls: int = 0

    # ---- accounting ----

    def record(self, *, cost_usd: float = 0.0, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.cost_usd += cost_usd
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.model_calls += 1

    def merge_external(self, cost_usd: float, tokens_in: int, tokens_out: int) -> None:
        self.cost_usd += cost_usd
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out

    # ---- queries ----

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.started_at

    def remaining_usd(self) -> float:
        return max(0.0, self.config.max_token_budget - self.cost_usd)

    def remaining_seconds(self) -> float:
        return max(0.0, self.config.max_runtime_seconds - self.elapsed_seconds)

    def exhausted(self) -> str:
        """Return the name of the first exhausted budget, or '' if healthy."""
        if self.cost_usd >= self.config.max_token_budget:
            return "BUDGET_EXCEEDED"
        if self.elapsed_seconds >= self.config.max_runtime_seconds:
            return "RUNTIME_LIMIT"
        return ""

    def estimate_cycles_remaining(self) -> int:
        """Rough forecast: how many more full cycles the budget can afford.

        Uses observed cost per completed task; conservative and advisory only.
        """
        if self.model_calls == 0:
            return -1
        per_call = self.cost_usd / max(1, self.model_calls)
        if per_call <= 0:
            return -1
        return int(self.remaining_usd() / per_call)

    def snapshot(self) -> dict[str, float | int | str]:
        return {
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "max_runtime_seconds": self.config.max_runtime_seconds,
            "cost_usd": round(self.cost_usd, 4),
            "max_token_budget": self.config.max_token_budget,
            "remaining_usd": round(self.remaining_usd(), 4),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "model_calls": self.model_calls,
            "max_parallel_agents": self.config.max_parallel_agents,
            "at": now_iso(),
        }
