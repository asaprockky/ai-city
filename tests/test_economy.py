from sqlalchemy import select

from asapville.agents.schemas import ActionName, Decision
from asapville.db.models import Agent, Event, Location, WorldState
from asapville.world import actions, economy
from asapville.world.perception import build_perception, render_perception


def _ctx(world, session, tick=0):
    import random

    from asapville.world.actions import TickContext

    return TickContext(session=session, settings=world.settings, tick=tick, time=world.clock.at(tick),
                       rng=random.Random(1),
                       locations={loc.id: loc for loc in session.scalars(select(Location))})


def _put(ctx, agent, key):
    agent.location_id = ctx.by_key[key].id
    agent.destination_id, agent.travel_ticks_left = None, 0


async def test_init_world_opens_the_shops(world, sessions, settings):
    with sessions() as s:
        ws = s.get(WorldState, 1)
        assert ws.treasury_cents == settings.economy.treasury_cents
        bakery = s.scalar(select(Location).where(Location.key == "bakery"))
        assert economy.stock_qty(s, bakery.id, "bread") == 24
        notice = s.scalar(select(Event).where(Event.kind == "notice"))
        assert notice is not None and notice.visibility == "public"
        # opening twice changes nothing
        assert economy.open_economy(s, settings, tick=0) is False


async def test_buy_moves_money_and_bread(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        a = s.scalars(select(Agent).order_by(Agent.id)).first()
        _put(ctx, a, "bakery")
        a.money_cents, a.inventory = 1000, {}
        d = Decision(thought="hungry", action=ActionName.BUY, action_args={"item": "bread", "qty": 2})
        assert actions.validate(ctx, a, d, set()) is None
        actions.execute(ctx, a, d)
        assert a.money_cents == 400 and a.inventory == {"bread": 2}
        assert economy.stock_qty(s, ctx.by_key["bakery"].id, "bread") == 22
        assert s.get(WorldState, 1).treasury_cents == settings.economy.treasury_cents + 600
        # too poor, wrong shop, empty shelf
        a.money_cents = 100
        assert "costs" in actions.validate(ctx, a, d, set())
        _put(ctx, a, "market")
        assert "Bakery sells it" in actions.validate(ctx, a, d, set())
        _put(ctx, a, "tavern")
        a.money_cents, a.hunger = 500, 90
        stew = Decision(thought="", action=ActionName.BUY, action_args={"item": "stew"})
        assert actions.validate(ctx, a, stew, set()) is None
        actions.execute(ctx, a, stew)
        assert a.hunger == 45 and "stew" not in a.inventory and a.money_cents == 0


async def test_jobs_and_the_production_chain(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        ws = s.get(WorldState, 1)
        a = s.scalars(select(Agent).order_by(Agent.id)).first()
        _put(ctx, a, "mill")
        work = Decision(thought="", action=ActionName.WORK)
        assert "no job" in actions.validate(ctx, a, work, set()).lower()
        apply = Decision(thought="", action=ActionName.APPLY, action_args={"job": "baker"})
        assert "not here" in actions.validate(ctx, a, apply, set())
        apply = Decision(thought="", action=ActionName.APPLY, action_args={"job": "miller"})
        assert actions.validate(ctx, a, apply, set()) is None
        actions.execute(ctx, a, apply)
        assert a.role == "miller"
        money, fund = a.money_cents, ws.treasury_cents
        actions.execute(ctx, a, work)  # 3 grain -> 3 flour
        assert a.money_cents == money + 300 and ws.treasury_cents == fund - 300
        assert economy.stock_qty(s, ctx.by_key["farm"].id, "grain") == 9
        # the miller delivers flour straight to the bakery
        assert economy.stock_qty(s, ctx.by_key["bakery"].id, "flour") == 9
        # run the farm dry: the mill stops and the worker is not paid
        economy.stock_add(s, ctx.by_key["farm"].id, "grain", -9)
        actions.execute(ctx, a, work)
        assert a.money_cents == money + 300
        ev = s.scalars(select(Event).where(Event.kind == "work").order_by(Event.id.desc())).first()
        assert ev.data["success"] is False and ev.data["missing"] == "grain"
        # a farmer refills it; vegetables go straight to the market
        b = list(s.scalars(select(Agent).order_by(Agent.id)))[1]
        _put(ctx, b, "farm")
        actions.execute(ctx, b, Decision(thought="", action=ActionName.APPLY, action_args={"job": "farmer"}))
        actions.execute(ctx, b, work)
        assert economy.stock_qty(s, ctx.by_key["farm"].id, "grain") == 3
        assert economy.stock_qty(s, ctx.by_key["market"].id, "vegetables") == 22
        actions.execute(ctx, b, Decision(thought="", action=ActionName.QUIT))
        assert b.role == "unemployed"


async def test_sell_and_notice(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        a = s.scalars(select(Agent).order_by(Agent.id)).first()
        _put(ctx, a, "market")
        a.inventory, a.money_cents = {"berries": 3}, 0
        sell = Decision(thought="", action=ActionName.SELL, action_args={"item": "berries", "qty": 2})
        assert actions.validate(ctx, a, sell, set()) is None
        actions.execute(ctx, a, sell)
        assert a.money_cents == 96 and a.inventory == {"berries": 1}  # 60% of 0.80 x 2
        assert economy.stock_qty(s, ctx.by_key["market"].id, "berries") == 2
        note = Decision(thought="", action=ActionName.WRITE_NOTICE, action_args={"text": "Berries for sale"})
        assert "Town Hall" in actions.validate(ctx, a, note, set())
        _put(ctx, a, "square")
        assert actions.validate(ctx, a, note, set()) is None
        actions.execute(ctx, a, note)
        other = list(s.scalars(select(Agent).order_by(Agent.id)))[1]
        _put(ctx, other, "bakery")
        p = build_perception(ctx, other)
        assert any(n.text == "Berries for sale" for n in p.notices)
        text = render_perception(p)
        assert "For sale here: bread 3.00 asaps (24 left)" in text
        assert "Jobs here: baker (3.00/h, 0 working)" in text
        assert "market: Market" in text and "sells vegetables, berries, fish" in text
        assert "On the town board:" in text


async def test_fake_town_trades_and_works(world, sessions):
    await world.run(ticks=48, realtime=False)
    with sessions() as s:
        kinds = {k for (k,) in s.execute(select(Event.kind).distinct())}
        assert {"buy", "hired", "work"} <= kinds
        assert s.get(WorldState, 1).treasury_cents != world.settings.economy.treasury_cents


async def test_wages_follow_the_labour_market(world, sessions, settings):
    with sessions() as s:
        agents = list(s.scalars(select(Agent).order_by(Agent.id)))
        for a in agents[:4]:
            a.role = "baker"
        changed = economy.adjust_wages(s, settings)
        assert changed["farmer"] == (250, 312) and changed["baker"] == (300, 270)
        assert "quarryman" in changed and "miller" in changed
        for _ in range(20):
            economy.adjust_wages(s, settings)
        assert economy.wage_of(s, settings, "farmer") == 750  # capped at 3x
        assert economy.wage_of(s, settings, "baker") == 150  # floored at 0.5x
        ctx = _ctx(world, s)
        _put(ctx, agents[4], "farm")
        text = render_perception(build_perception(ctx, agents[4]))
        assert "farmer (7.50/h, 0 working)" in text


async def test_a_bottleneck_raises_its_own_wage(world, sessions, settings):
    from asapville.world.events import emit

    with sessions() as s:
        agents = list(s.scalars(select(Agent).order_by(Agent.id)))
        agents[0].role = "miller"
        for a in agents[1:5]:
            a.role = "baker"
        for t in range(4):  # bakers idle for want of flour, while a miller is on the books
            emit(s, tick=t, kind="work", actor=agents[1], visibility="local", summary="no flour",
                 data={"job": "baker", "success": False, "missing": "flour"})
        changed = economy.adjust_wages(s, settings, tick=10)
        assert changed["miller"] == (300, 375)
        assert "baker" not in changed or changed["baker"][1] < changed["baker"][0]


async def test_a_shortage_is_blamed_on_the_first_idle_link_of_the_chain(world, sessions, settings):
    from asapville.world.events import emit

    with sessions() as s:
        agents = list(s.scalars(select(Agent).order_by(Agent.id)))
        agents[0].role = "farmer"
        for a in agents[1:5]:
            a.role = "miller"
        for t in range(4):  # bakers want flour, but the millers themselves stand idle for grain
            emit(s, tick=t, kind="work", actor=agents[1], visibility="local", summary="no flour",
                 data={"job": "baker", "success": False, "missing": "flour"})
            emit(s, tick=t, kind="work", actor=agents[2], visibility="local", summary="no grain",
                 data={"job": "miller", "success": False, "missing": "grain"})
        assert economy.shortages(s, settings, 0) == {"farmer": 8}
        changed = economy.adjust_wages(s, settings, tick=10)
        assert changed["farmer"][1] > changed["farmer"][0]
        assert changed["miller"][1] < changed["miller"][0]  # four millers, none of them short
