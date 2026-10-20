import random

from sqlalchemy import select

from asapville.agents.schemas import ActionName, Decision
from asapville.db.models import Agent, BankAccount, Event, Location, WorldState
from asapville.world import actions, bank
from asapville.world.actions import TickContext
from asapville.world.events import emit
from asapville.world.perception import build_perception, render_perception


def _ctx(world, session, tick=0):
    return TickContext(session=session, settings=world.settings, tick=tick, time=world.clock.at(tick),
                       rng=random.Random(1),
                       locations={loc.id: loc for loc in session.scalars(select(Location))})


def _at_bank(ctx, agent):
    agent.location_id, agent.destination_id, agent.travel_ticks_left = ctx.by_key["bank"].id, None, 0
    agent.asleep = False


def _do(ctx, agent, action, **args):
    d = Decision(thought="", action=action, action_args=args)
    err = actions.validate(ctx, agent, d, set())
    if err is None:
        actions.execute(ctx, agent, d)
    return err


async def test_savings_earn_interest_and_stay_in_the_vault(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        a = s.get(Agent, 1)
        _at_bank(ctx, a)
        a.money_cents = 10_000
        fund = s.get(WorldState, 1).treasury_cents
        assert _do(ctx, a, ActionName.DEPOSIT, asaps=50) is None
        assert a.money_cents == 5000 and s.get(WorldState, 1).treasury_cents == fund + 5000
        bank.daily(s, settings, 24)
        assert s.get(BankAccount, a.id).deposit_cents == 5025  # 0.5% a day
        assert "savings 50.25 asaps" in render_perception(build_perception(ctx, a))
        assert "only have" in _do(ctx, a, ActionName.DEPOSIT, asaps=999)
        assert _do(ctx, a, ActionName.WITHDRAW, asaps=20) is None and a.money_cents == 7000


async def test_credit_follows_income_and_businesses(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s, tick=50)
        a = s.get(Agent, 1)
        _at_bank(ctx, a)
        a.role, a.money_cents = "unemployed", 0
        assert bank.can_borrow(s, settings, a, 50) == 0
        assert "at most 0.00" in _do(ctx, a, ActionName.BORROW, asaps=10)
        a.role = "miller"
        for t in range(10):
            emit(s, tick=40 + t, kind="work", actor=a, summary="worked",
                 data={"job": "miller", "success": True, "wage_cents": 800})
        assert bank.credit_limit(s, settings, a, 50) == 2000 + 2 * 8000
        ctx.by_key["mill"].owner_id = a.id
        assert bank.credit_limit(s, settings, a, 50) == 2000 + 16_000 + 20_000
        assert "borrow up to 380.00" in render_perception(build_perception(ctx, a))


async def test_a_loan_grows_and_an_unpaid_one_costs_the_business(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s, tick=0)
        a = s.get(Agent, 1)
        _at_bank(ctx, a)
        a.role, a.money_cents = "miller", 0
        mill = ctx.by_key["mill"]
        mill.owner_id = a.id
        assert _do(ctx, a, ActionName.BORROW, asaps=150) is None
        acc = s.get(BankAccount, a.id)
        assert a.money_cents == 15_000 and acc.loan_cents == 15_000 and acc.loan_due_tick == 7 * 24
        bank.daily(s, settings, 24)
        assert acc.loan_cents == 15_300  # 2% a day
        assert _do(ctx, a, ActionName.REPAY, asaps=100) is None and acc.loan_cents == 5300
        a.money_cents = 1000
        bank.daily(s, settings, 7 * 24)  # overdue: cash first, then the mill
        assert a.money_cents == 0 and mill.owner_id is None and acc.loan_cents == 0
        ev = s.scalar(select(Event).where(Event.kind == "notice", Event.agent_id == a.id))
        assert "Mill" in ev.summary


async def test_a_default_freezes_credit(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s, tick=0)
        a = s.get(Agent, 1)
        _at_bank(ctx, a)
        a.role, a.money_cents = "miller", 0
        assert _do(ctx, a, ActionName.BORROW, asaps=20) is None
        a.money_cents = 0
        bank.daily(s, settings, 7 * 24)
        acc = s.get(BankAccount, a.id)
        assert acc.defaulted and acc.loan_cents > 0
        assert bank.can_borrow(s, settings, a, 7 * 24) == 0
        assert "defaulted" in render_perception(build_perception(ctx, a))
