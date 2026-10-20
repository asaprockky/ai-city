import random

from sqlalchemy import select

from asapville.agents.schemas import ActionName, Decision
from asapville.db.models import Agent, Brand, Event, Location, Memory, Stall, WorldState
from asapville.world import actions
from asapville.world.actions import TickContext
from asapville.world.perception import build_perception, render_perception


def _ctx(world, session, tick=0):
    return TickContext(session=session, settings=world.settings, tick=tick, time=world.clock.at(tick),
                       rng=random.Random(1),
                       locations={loc.id: loc for loc in session.scalars(select(Location))})


def _at(ctx, agent, key):
    agent.location_id, agent.destination_id, agent.travel_ticks_left, agent.asleep = \
        ctx.by_key[key].id, None, 0, False


async def test_a_citizen_runs_a_business(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        ruth, buyer = list(s.scalars(select(Agent).order_by(Agent.id)))[:2]
        _at(ctx, ruth, "tavern")
        _at(ctx, buyer, "tavern")
        ruth.inventory, ruth.money_cents = {"bread": 6}, 0
        buyer.money_cents, buyer.inventory = 2000, {}
        fund = s.get(WorldState, 1).treasury_cents
        s.flush()

        open_ = Decision(thought="", action=ActionName.OPEN_STALL,
                         action_args={"name": "Ruth's Late Loaves", "item": "Bread", "price": 4.5})
        assert actions.validate(ctx, ruth, open_, set()) is None
        actions.execute(ctx, ruth, open_)
        stall = s.scalar(select(Stall))
        assert (stall.item, stall.price_cents, stall.qty) == ("bread", 450, 0)
        assert s.scalar(select(Brand).where(Brand.name == "Ruth's Late Loaves")).owner_id == ruth.id
        assert "already sell" in actions.validate(ctx, ruth, open_, set())

        stock = Decision(thought="", action=ActionName.STOCK_STALL, action_args={"item": "bread", "qty": 5})
        assert actions.validate(ctx, ruth, stock, set()) is None
        actions.execute(ctx, ruth, stock)
        assert stall.qty == 5 and ruth.inventory == {"bread": 1}

        text = render_perception(build_perception(ctx, buyer))
        assert "Citizens' stalls here: bread 4.50 asaps (5 left)" in text and "Ruth's Late Loaves" in text
        assert "Your own businesses" in render_perception(build_perception(ctx, ruth))

        # the tavern sells stew itself, but bread only from Ruth: her price, her till
        buy = Decision(thought="", action=ActionName.BUY, action_args={"item": "bread", "qty": 2})
        assert actions.validate(ctx, buyer, buy, set()) is None
        actions.execute(ctx, buyer, buy)
        assert buyer.inventory == {"bread": 2} and buyer.money_cents == 1100
        assert ruth.money_cents == 900 and stall.qty == 3 and stall.sold == 2
        assert s.get(WorldState, 1).treasury_cents == fund  # the town took nothing
        memories = s.scalars(select(Memory).where(Memory.agent_id == ruth.id))
        assert any("sold 2 bread" in m.content for m in memories)

        too_many = Decision(thought="", action=ActionName.BUY, action_args={"item": "bread", "qty": 9})
        assert "only 3" in actions.validate(ctx, buyer, too_many, set())
        wrong = Decision(thought="", action=ActionName.BUY,
                         action_args={"item": "bread", "from": "Nobody's Shop"})
        assert "no stall called" in actions.validate(ctx, buyer, wrong, set())
        reprice = Decision(thought="", action=ActionName.STOCK_STALL,
                           action_args={"item": "bread", "price": 6})
        actions.execute(ctx, ruth, reprice)
        assert stall.price_cents == 600
        assert s.scalar(select(Event).where(Event.kind == "open_stall")).visibility == "public"


async def test_stall_rules(world, sessions):
    with sessions() as s:
        ctx = _ctx(world, s)
        a, b = list(s.scalars(select(Agent).order_by(Agent.id)))[:2]
        _at(ctx, a, "square")
        d = lambda **kw: Decision(thought="", action=ActionName.OPEN_STALL, action_args=kw)  # noqa: E731
        assert "price" in actions.validate(ctx, a, d(name="X Co", item="spoon", price=0), set())
        assert "Name your business" in actions.validate(ctx, a, d(name="", item="spoon", price=1), set())
        a.location_id = b.home_id
        home_co = d(name="Home Co", item="spoon", price=1)
        assert "someone else's home" in actions.validate(ctx, a, home_co, set())
        a.location_id = a.home_id
        assert actions.validate(ctx, a, d(name="Home Co", item="spoon", price=1), set()) is None
        nothing = Decision(thought="", action=ActionName.STOCK_STALL, action_args={"item": "spoon", "qty": 1})
        assert "no stall" in actions.validate(ctx, a, nothing, set()).lower()


async def test_empty_shops_can_be_bought_and_only_the_owner_trades_there(world, sessions, settings):
    from asapville.world.perception import build_perception, render_perception

    with sessions() as s:
        ctx = _ctx(world, s)
        a, b = list(s.scalars(select(Agent).order_by(Agent.id)))[:2]
        _at(ctx, a, "well_shop")
        _at(ctx, b, "well_shop")
        shop = ctx.by_key["well_shop"]
        assert shop.kind == "shop" and shop.owner_id is None
        assert "for sale" in render_perception(build_perception(ctx, a))
        open_ = Decision(thought="", action=ActionName.OPEN_STALL,
                         action_args={"name": "Hester's Cooperage", "item": "barrel", "price": 12})
        assert "for sale" in actions.validate(ctx, a, open_, set())
        buy = Decision(thought="", action=ActionName.BUY_PROPERTY)
        a.money_cents = 100
        assert "costs 300.00" in actions.validate(ctx, a, buy, set())
        a.money_cents = 50_000
        fund = s.get(WorldState, 1).treasury_cents
        actions.execute(ctx, a, buy)
        assert shop.owner_id == a.id and a.money_cents == 20_000
        assert s.get(WorldState, 1).treasury_cents == fund + 30_000
        assert actions.validate(ctx, a, open_, set()) is None
        assert "belongs to" in actions.validate(ctx, b, open_, set())
        b.money_cents = 50_000
        assert "already belongs" in actions.validate(ctx, b, buy, set())
        assert "your own shop" in render_perception(build_perception(ctx, a))
