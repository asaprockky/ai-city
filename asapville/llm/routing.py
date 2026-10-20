"""Model routing. `model_for` is the ONLY place that decides which model runs.

Tier is a world rule (`tier_for`): role and wealth decide it, re-evaluated each
simulated day, so promotion/demotion automatically changes the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from asapville.config import EffortCfg, ModelNames, TiersCfg

ANGEL, NOTABLE, MANAGER, WORKER = 0, 1, 2, 3
TIER_NAMES = {ANGEL: "angel", NOTABLE: "notable", MANAGER: "manager", WORKER: "worker"}

_DEFAULT_MODELS = ModelNames()
_DEFAULT_EFFORT = EffortCfg()


class HasTier(Protocol):
    tier: int


@dataclass(frozen=True)
class AngelTask:
    """The routing subject for angel work (angels are not citizens, have no DB row)."""

    role: str  # watcher | fixer | judge
    severity: int = 1
    failed_fixes: int = 0
    tier: int = ANGEL

    @property
    def needs_opus(self) -> bool:
        return self.role == "fixer" and (self.severity >= 4 or self.failed_fixes >= 2)


def model_for(subject: HasTier, models: ModelNames = _DEFAULT_MODELS) -> str:
    if subject.tier == ANGEL:
        if isinstance(subject, AngelTask) and subject.needs_opus:
            return models.opus
        return models.sonnet
    if subject.tier in (NOTABLE, MANAGER):
        return models.sonnet
    return models.haiku


def effort_for(subject: HasTier, effort: EffortCfg = _DEFAULT_EFFORT) -> str | None:
    return getattr(effort, TIER_NAMES.get(subject.tier, "worker"))


def tier_for(role: str, money_cents: int, cfg: TiersCfg, *, owns_business: bool = False) -> int:
    """Citizen tier from role, wealth and influence."""
    if role in cfg.notable_roles or money_cents >= cfg.notable_wealth_cents:
        return NOTABLE
    if role in cfg.manager_roles or owns_business or money_cents >= cfg.manager_wealth_cents:
        return MANAGER
    return WORKER
