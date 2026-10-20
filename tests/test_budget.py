import pytest

from asapville.config import BudgetCfg
from asapville.db.models import LLMCall
from asapville.llm.budget import NORMAL, PAUSED, SLOW, BudgetExceeded, BudgetGuard


def guard(day="2026-10-20", **kw):
    holder = {"day": day}
    g = BudgetGuard(BudgetCfg(daily_usd=1.0, per_agent_daily_usd=0.3, angel_opus_monthly_usd=0.5,
                              **kw), today=lambda: holder["day"])
    return g, holder


def test_modes_follow_daily_spend():
    g, _ = guard()
    assert g.mode == NORMAL
    g.record(0.89, agent_id=None, model="m", angel_role=None)
    assert g.mode == NORMAL
    g.record(0.02, agent_id=None, model="m", angel_role=None)
    assert g.mode == SLOW
    g.record(0.10, agent_id=None, model="m", angel_role=None)
    assert g.mode == PAUSED
    with pytest.raises(BudgetExceeded) as e:
        g.check(agent_id=None, model="m", angel_role=None)
    assert e.value.scope == "daily"


def test_per_agent_cap_blocks_only_that_agent():
    g, _ = guard()
    g.record(0.3, agent_id=7, model="m", angel_role=None)
    with pytest.raises(BudgetExceeded) as e:
        g.check(agent_id=7, model="m", angel_role=None)
    assert e.value.scope == "agent"
    g.check(agent_id=8, model="m", angel_role=None)
    assert g.agent_on_fallback(7) and not g.agent_on_fallback(8)
    assert not g.agent_capped(7)
    g.check(agent_id=7, model=g.cheap_model, angel_role=None)  # past the cap: the cheap model still thinks
    g.record(g.cfg.fallback_agent_daily_usd, agent_id=7, model=g.cheap_model, angel_role=None)
    assert g.agent_capped(7)
    with pytest.raises(BudgetExceeded):
        g.check(agent_id=7, model=g.cheap_model, angel_role=None)


def test_opus_monthly_cap_is_separate():
    g, _ = guard()
    g.record(0.5, agent_id=None, model="claude-opus-5-5", angel_role="fixer")
    with pytest.raises(BudgetExceeded) as e:
        g.check(agent_id=None, model="claude-opus-5-5", angel_role="fixer")
    assert e.value.scope == "opus_monthly"
    g.check(agent_id=None, model="claude-sonnet-5-5", angel_role="fixer")


def test_new_day_resets_daily_but_not_monthly():
    g, holder = guard()
    g.record(0.95, agent_id=1, model="claude-opus-5-5", angel_role="fixer")
    assert g.mode == SLOW
    holder["day"] = "2026-10-21"
    assert g.mode == NORMAL and not g.agent_capped(1)
    assert g.opus_month == pytest.approx(0.95)
    holder["day"] = "2026-11-01"
    g.check(agent_id=None, model="claude-sonnet-5-5", angel_role=None)
    assert g.opus_month == 0


def test_load_rebuilds_from_llm_calls(sessions, settings):
    from asapville.db.session import upgrade_to_head

    upgrade_to_head(settings.db_url)
    with sessions() as s:
        s.add_all([
            LLMCall(real_day="2026-10-20", tick=1, agent_id=None, purpose="decide", backend="x",
                    model="claude-haiku-5-5", tier=3, cost_usd=0.4),
            LLMCall(real_day="2026-10-19", tick=1, agent_id=None, purpose="decide", backend="x",
                    model="claude-haiku-5-5", tier=3, cost_usd=9.0),
        ])
        s.commit()
        g, _ = guard()
        g.load(s)
    assert g.spent_today == pytest.approx(0.4)
