"""Budget guard: hard daily cap, hard per-agent cap, monthly angel-Opus cap.

Spend is keyed by real UTC day (it's real money). At `slow_mode_threshold` of the
daily cap the world enters slow mode; at 100% it pauses and logs loudly.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from asapville.config import BudgetCfg
from asapville.db.models import LLMCall

log = logging.getLogger("asapville.budget")

NORMAL, SLOW, PAUSED = "normal", "slow", "budget_paused"


def utc_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class BudgetExceeded(RuntimeError):
    def __init__(self, scope: str, detail: str) -> None:
        super().__init__(f"budget exceeded ({scope}): {detail}")
        self.scope = scope  # daily | agent | opus_monthly


class BudgetGuard:
    def __init__(self, cfg: BudgetCfg, today: Callable[[], str] = utc_today,
                 opus_model: str = "claude-opus-5-5") -> None:
        self.cfg = cfg
        self._today = today
        self.opus_model = opus_model
        self.day = today()
        self.spent_today = 0.0
        self.spent_by_agent: dict[int, float] = defaultdict(float)
        self.opus_month = 0.0

    # -- state -------------------------------------------------------------
    def load(self, session: Session) -> None:
        """Rebuild counters from llm_calls (so restarts don't reset the cap)."""
        self.day = self._today()
        self.spent_today = float(session.scalar(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(LLMCall.real_day == self.day)
        ) or 0.0)
        self.spent_by_agent = defaultdict(float)
        rows = session.execute(
            select(LLMCall.agent_id, func.sum(LLMCall.cost_usd))
            .where(LLMCall.real_day == self.day, LLMCall.agent_id.is_not(None))
            .group_by(LLMCall.agent_id)
        )
        for agent_id, total in rows:
            self.spent_by_agent[agent_id] = float(total or 0.0)
        month = self.day[:7]
        self.opus_month = float(session.scalar(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(
                LLMCall.real_day.like(f"{month}%"), LLMCall.model == self.opus_model,
                LLMCall.angel_role.is_not(None))
        ) or 0.0)

    def _roll_day(self) -> None:
        today = self._today()
        if today != self.day:
            if today[:7] != self.day[:7]:
                self.opus_month = 0.0
            self.day = today
            self.spent_today = 0.0
            self.spent_by_agent = defaultdict(float)

    def record(self, cost_usd: float, *, agent_id: int | None, model: str,
               angel_role: str | None) -> None:
        self._roll_day()
        self.spent_today += cost_usd
        if agent_id is not None:
            self.spent_by_agent[agent_id] += cost_usd
        if angel_role is not None and model == self.opus_model:
            self.opus_month += cost_usd

    # -- decisions ---------------------------------------------------------
    @property
    def fraction_used(self) -> float:
        self._roll_day()
        return self.spent_today / self.cfg.daily_usd if self.cfg.daily_usd > 0 else 1.0

    @property
    def mode(self) -> str:
        f = self.fraction_used
        if f >= 1.0:
            return PAUSED
        if f >= self.cfg.slow_mode_threshold:
            return SLOW
        return NORMAL

    def check(self, *, agent_id: int | None, model: str, angel_role: str | None) -> None:
        """Raise BudgetExceeded if this call must not happen."""
        if self.mode == PAUSED:
            log.error("DAILY BUDGET EXHAUSTED: $%.4f of $%.2f spent on %s",
                      self.spent_today, self.cfg.daily_usd, self.day)
            raise BudgetExceeded("daily", f"${self.spent_today:.4f} >= ${self.cfg.daily_usd:.2f}")
        if agent_id is not None and self.spent_by_agent[agent_id] >= self.cfg.per_agent_daily_usd:
            raise BudgetExceeded("agent", f"agent {agent_id} spent "
                                 f"${self.spent_by_agent[agent_id]:.4f} today")
        if (angel_role is not None and model == self.opus_model
                and self.opus_month >= self.cfg.angel_opus_monthly_usd):
            raise BudgetExceeded("opus_monthly", f"${self.opus_month:.4f} this month")

    def agent_capped(self, agent_id: int) -> bool:
        self._roll_day()
        return self.spent_by_agent[agent_id] >= self.cfg.per_agent_daily_usd
