from asapville.config import ModelNames, TiersCfg
from asapville.llm.routing import AngelTask, effort_for, model_for, tier_for


class Sub:
    def __init__(self, tier):
        self.tier = tier


def test_citizen_tiers():
    assert model_for(Sub(1)) == "claude-sonnet-5-5"
    assert model_for(Sub(2)) == "claude-sonnet-5-5"
    assert model_for(Sub(3)) == "claude-haiku-5-5"
    assert effort_for(Sub(1)) == "medium" and effort_for(Sub(2)) == "low"
    assert effort_for(Sub(3)) is None


def test_angels_escalate_to_opus_only_when_needed():
    assert model_for(AngelTask("watcher", severity=5)) == "claude-sonnet-5-5"
    assert model_for(AngelTask("judge")) == "claude-sonnet-5-5"
    assert model_for(AngelTask("fixer", severity=3)) == "claude-sonnet-5-5"
    assert model_for(AngelTask("fixer", severity=4)) == "claude-opus-5-5"
    assert model_for(AngelTask("fixer", severity=2, failed_fixes=2)) == "claude-opus-5-5"
    assert effort_for(AngelTask("watcher")) == "high"


def test_custom_model_names_are_respected():
    names = ModelNames(opus="o", sonnet="s", haiku="h")
    assert model_for(Sub(3), names) == "h"


def test_tier_from_role_and_wealth():
    cfg = TiersCfg(notable_roles=["mayor"], manager_roles=["shopkeeper"],
                   notable_wealth_cents=500_000, manager_wealth_cents=100_000)
    assert tier_for("unemployed", 0, cfg) == 3
    assert tier_for("shopkeeper", 0, cfg) == 2
    assert tier_for("farmhand", 0, cfg, owns_business=True) == 2
    assert tier_for("farmhand", 150_000, cfg) == 2
    assert tier_for("mayor", 0, cfg) == 1
    assert tier_for("farmhand", 600_000, cfg) == 1
