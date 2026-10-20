import random

from sqlalchemy import select

from asapville.agents.schemas import ActionName, Decision
from asapville.db.models import Agent, Event, Location, WorldState
from asapville.world import actions, economy
from asapville.world.actions import TickContext
from asapville.world.perception import build_perception, render_perception


def _ctx(world, session, tick=0):
    return TickContext(session=session, settings=world.settings, tick=tick, time=world.clock.at(tick),
                       rng=random.Random(1),
                       locations={loc.id: loc for loc in session.scalars(select(Location))})


def _put(ctx, agent, key):
    agent.location_id, agent.destination_id, agent.travel_ticks_left = ctx.by_key[key].id, None, 0
    agent.asleep, agent.energy = False, 80


def _do(ctx, agent, action, **args):
    d = Decision(thought="", action=action, action_args=args)
    err = actions.validate(ctx, agent, d, set())
    if err is None:
        actions.execute(ctx, agent, d)
    return err


async def test_a_citizen_buys_the_bakery_and_runs_it(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        owner, baker, buyer = list(s.scalars(select(Agent).order_by(Agent.id)))[:3]
        bakery = ctx.by_key["bakery"]
        owner.money_cents = 100_000
        _put(ctx, owner, "bakery")
        assert "for sale, 600 asaps" in render_perception(build_perception(ctx, owner))
        assert _do(ctx, owner, ActionName.BUY_PROPERTY) is None
        assert bakery.owner_id == owner.id and owner.money_cents == 40_000

        # the owner sets the wage; the town's daily wage adjustment leaves it alone
        assert _do(ctx, owner, ActionName.HIRE, job="baker", wage=4.0) is None
        assert economy.wage_of(s, settings, "baker") == 400
        assert "baker" not in economy.adjust_wages(s, settings, tick=10)

        # a baker's wage comes out of the owner's pocket, not the town fund
        baker.role = "baker"
        _put(ctx, baker, "bakery")
        ws = s.get(WorldState, 1)
        fund, purse, pay = ws.treasury_cents, owner.money_cents, baker.money_cents
        assert _do(ctx, baker, ActionName.WORK) is None
        assert baker.money_cents == pay + 380 and owner.money_cents == purse - 400  # 5% income tax
        assert ws.treasury_cents == fund + 20
        assert "for " + owner.name + ", who owns it" in render_perception(build_perception(ctx, baker))

        # bread sales go to the owner, at the owner's price
        assert _do(ctx, owner, ActionName.SET_PRICE, item="bread", to=3.5) is None
        _put(ctx, buyer, "bakery")
        buyer.money_cents, purse = 1000, owner.money_cents
        assert _do(ctx, buyer, ActionName.BUY, item="bread") is None
        assert buyer.money_cents == 650 and owner.money_cents == purse + 315  # 10% sales tax
        assert economy.price_of(settings, "bread", s, at="market") == 300  # only at the bakery

        # the town can't vote on a wage a citizen pays
        _put(ctx, buyer, "town_hall")
        err = _do(ctx, buyer, ActionName.PROPOSE_LAW, text="bakers deserve more",
                  change={"wage": "baker", "to": 9})
        assert err is not None and owner.name in err

        assert _do(ctx, owner, ActionName.FIRE, who=baker.id) is None
        assert baker.role == "unemployed"
        assert s.scalar(select(Event).where(Event.kind == "fired")).target_id == baker.id


async def test_a_broke_owner_cannot_pay_and_deliveries_are_bought(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        owner, miller = list(s.scalars(select(Agent).order_by(Agent.id)))[:2]
        ctx.by_key["bakery"].owner_id = owner.id
        owner.money_cents = 0
        miller.role = "miller"
        _put(ctx, miller, "mill")
        farm, bakery = ctx.by_key["farm"], ctx.by_key["bakery"]
        economy.stock_add(s, farm.id, "grain", 6)
        # the town's mill delivers flour to the citizen's bakery, which must pay for it
        flour = economy.stock_qty(s, bakery.id, "flour")
        assert _do(ctx, miller, ActionName.WORK) is None
        assert economy.stock_qty(s, bakery.id, "flour") == flour + 3
        assert owner.money_cents == 0  # paid what it could: nothing

        baker = list(s.scalars(select(Agent).order_by(Agent.id)))[2]
        baker.role = "baker"
        _put(ctx, baker, "bakery")
        pay = baker.money_cents
        assert _do(ctx, baker, ActionName.WORK) is None
        assert baker.money_cents == pay
        ev = s.scalar(select(Event).where(Event.kind == "work", Event.agent_id == baker.id))
        assert ev.data["missing"] == "wages" and owner.name in ev.summary


async def test_owning_a_business_makes_a_manager(world, sessions, settings):
    from asapville.llm.routing import MANAGER

    with sessions() as s:
        a = s.get(Agent, 1)
        a.role, a.money_cents = "unemployed", 100
        s.scalar(select(Location).where(Location.key == "mill")).owner_id = a.id
        ws = s.get(WorldState, 1)
        ws.tick = 18  # midnight: the step runs the daily round
        s.commit()
    await world.step()
    with sessions() as s:
        assert s.get(Agent, 1).tier == MANAGER
