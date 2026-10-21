from sqlalchemy import select

from asapville.config import BudgetCfg
from asapville.db.models import Agent
from asapville.llm.budget import BudgetExceeded, BudgetGuard
from asapville.llm.routing import WORKER, model_for


def test_a_main_hero_always_thinks_on_the_strong_model_up_to_their_own_cap():
    cfg = BudgetCfg(daily_usd=100, per_agent_daily_usd=1.0, fallback_agent_daily_usd=1.5,
                    lead_agent_daily_usd=2.0)
    guard = BudgetGuard(cfg, cheap_model="haiku")
    guard.leads = {1}
    guard.record(1.2, agent_id=1, model="sonnet", angel_role=None)
    guard.record(1.2, agent_id=2, model="sonnet", angel_role=None)
    assert not guard.agent_on_fallback(1) and guard.agent_on_fallback(2)
    guard.check(agent_id=1, model="sonnet", angel_role=None)  # past the usual cap, still the strong model
    guard.record(1.0, agent_id=1, model="sonnet", angel_role=None)
    assert guard.agent_capped(1)
    try:
        guard.check(agent_id=1, model="sonnet", angel_role=None)
        raise AssertionError("a hero past their own cap must rest")
    except BudgetExceeded:
        pass


async def test_heroes_route_to_sonnet_and_lead_the_reports(world, sessions, settings):
    from asapville import digest
    from asapville.world.events import emit

    models = settings.llm.models
    with sessions() as s:
        hero, extra = list(s.scalars(select(Agent).order_by(Agent.id)))[:2]
        hero.lead, hero.tier, extra.tier = True, WORKER, WORKER
        assert model_for(hero, models) == models.sonnet and model_for(extra, models) == models.haiku
        emit(s, tick=1, kind="talk", actor=hero, summary=f"{hero.name}: hello")
        for i in range(5):
            emit(s, tick=1, kind="talk", actor=extra, summary=f"{extra.name}: chatter {i}")
        emit(s, tick=1, kind="arrival", actor=extra, summary="newcomer", data={"lead": False})
        s.flush()
        text = digest.happenings(s, 0, others=2)
        facts = digest.facts(s, settings)
        arrival = s.scalar(select(digest.Event).where(digest.Event.kind == "arrival"))
        assert not digest.is_breaking(arrival)
    assert f"{hero.name}: hello" in text and "chatter 4" in text and "chatter 0" not in text
    assert f"- ★ {hero.name}," in facts
