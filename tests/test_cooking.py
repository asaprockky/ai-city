import random

from sqlalchemy import select

from asapville.agents.schemas import ActionName, Decision
from asapville.db.models import Agent, Event, Location
from asapville.world import actions, economy
from asapville.world.actions import TickContext
from asapville.world.perception import build_perception, render_perception


def _ctx(world, session, tick=0):
    return TickContext(session=session, settings=world.settings, tick=tick, time=world.clock.at(tick),
                       rng=random.Random(1),
                       locations={loc.id: loc for loc in session.scalars(select(Location))})


def _do(ctx, agent, action, **args):
    d = Decision(thought="", action=action, action_args=args)
    err = actions.validate(ctx, agent, d, set())
    if err is None:
        actions.execute(ctx, agent, d)
    return err


async def test_cook_pancakes_at_home_and_sell_them(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        a, b = list(s.scalars(select(Agent).order_by(Agent.id)))[:2]
        a.location_id, a.destination_id, a.travel_ticks_left = ctx.by_key["square"].id, None, 0
        a.asleep = False
        a.inventory, a.energy = {"flour": 1, "eggs": 1, "milk": 1}, 80
        assert "kitchen" in _do(ctx, a, ActionName.COOK, dish="pancakes")
        assert "could cook pancakes" in render_perception(build_perception(ctx, a))
        a.location_id = a.home_id
        assert "lack" in _do(ctx, a, ActionName.COOK, dish="fish pie")
        assert "Recipes:" in render_perception(build_perception(ctx, a))
        assert _do(ctx, a, ActionName.COOK, dish="pancakes") is None
        assert a.inventory == {"pancakes": 3}
        assert s.scalar(select(Event).where(Event.kind == "cook")).data["made"] == 3
        mood = a.mood = 50
        assert _do(ctx, a, ActionName.EAT, item="pancakes") is None
        assert a.mood == mood + 7  # a good dish lifts the mood
        # a small business: a stall of pancakes
        a.location_id = ctx.by_key["square"].id
        assert _do(ctx, a, ActionName.OPEN_STALL, name="Morning Pancakes", item="pancakes", price=2.5) is None


async def test_herders_and_fishers_stock_the_market(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        a, b = list(s.scalars(select(Agent).order_by(Agent.id)))[:2]
        market = ctx.by_key["market"]
        for agent, job, place in ((a, "herder", "farm"), (b, "fisher", "river")):
            agent.role, agent.energy, agent.asleep = job, 80, False
            agent.location_id, agent.destination_id, agent.travel_ticks_left = ctx.by_key[place].id, None, 0
            assert _do(ctx, agent, ActionName.WORK) is None
        assert economy.stock_qty(s, market.id, "eggs") == 3 and economy.stock_qty(s, market.id, "milk") == 3
        assert economy.stock_qty(s, ctx.by_key["farm"].id, "eggs") == 1
        assert economy.stock_qty(s, market.id, "fish") >= 3
