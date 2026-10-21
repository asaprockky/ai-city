import random

from sqlalchemy import select

from asapville.agents.schemas import ActionName, Decision
from asapville.db.models import Agent, Event, Location, Proposal, WorldState
from asapville.world import actions, democracy, economy
from asapville.world.actions import TickContext
from asapville.world.perception import build_perception, render_perception


def _ctx(world, session, tick=0):
    return TickContext(session=session, settings=world.settings, tick=tick, time=world.clock.at(tick),
                       rng=random.Random(1),
                       locations={loc.id: loc for loc in session.scalars(select(Location))})


def _at(ctx, agents, key):
    for a in agents:
        a.location_id, a.destination_id, a.travel_ticks_left, a.asleep = ctx.by_key[key].id, None, 0, False


def _do(ctx, agent, action, **args):
    d = Decision(thought="", action=action, action_args=args)
    err = actions.validate(ctx, agent, d, set())
    if err is None:
        actions.execute(ctx, agent, d)
    return err


async def test_the_town_votes_a_wage_into_law(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        people = list(s.scalars(select(Agent).order_by(Agent.id)))
        _at(ctx, people, "town_hall")
        a, b, c, d, e = people
        assert _do(ctx, a, ActionName.PROPOSE_LAW, text="Pay farmers properly",
                   change={"wage": "farmer", "to": 5.0}) is None
        assert "already have a proposal" in _do(ctx, a, ActionName.PROPOSE_LAW, text="Another one")
        p = s.scalar(select(Proposal))
        assert (p.kind, p.target, p.value_cents) == ("wage", "farmer", 500)
        assert p.closes_tick == democracy.VOTING_HOURS
        for voter, choice in ((a, "yes"), (b, "yes"), (c, "no"), (d, "yes")):
            assert _do(ctx, voter, ActionName.VOTE, proposal=p.id, choice=choice) is None
        assert _do(ctx, c, ActionName.VOTE, proposal=p.id, choice="yes") is None  # changed their mind
        assert (p.yes, p.no) == (4, 0)
        text = render_perception(build_perception(ctx, e))
        assert "Open proposals" in text and "Pay farmers properly" in text and "you haven't voted" in text
        assert democracy.close_due(s, democracy.VOTING_HOURS - 1) == []
        assert [x.id for x in democracy.close_due(s, democracy.VOTING_HOURS)] == [p.id]
        assert p.status == "passed"
        assert economy.wage_of(s, settings, "farmer") == 500
        law = s.scalar(select(Event).where(Event.kind == "law"))
        assert law.visibility == "public" and law.data["passed"] is True
        assert "Recently decided by the town" in render_perception(build_perception(ctx, e))
        assert "no open proposal" in _do(ctx, e, ActionName.VOTE, proposal=p.id, choice="yes")


async def test_prices_laws_quorum_and_places(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        people = list(s.scalars(select(Agent).order_by(Agent.id)))
        _at(ctx, people, "square")
        a, b, c = people[:3]
        free = {"price": "bread", "to": 0}
        assert "between" in _do(ctx, a, ActionName.PROPOSE_LAW, text="Free bread", change=free)
        two = {"price": "bread", "to": 2}
        assert _do(ctx, a, ActionName.PROPOSE_LAW, text="Bread for 2", change=two) is None
        assert _do(ctx, b, ActionName.PROPOSE_LAW, text="No shouting after dark") is None
        cheap, law = list(s.scalars(select(Proposal).order_by(Proposal.id)))
        for v in (a, b, c):
            _do(ctx, v, ActionName.VOTE, proposal=cheap.id, choice="yes")
        _do(ctx, a, ActionName.VOTE, proposal=law.id, choice="yes")  # one vote: below quorum
        democracy.close_due(s, 100)
        assert cheap.status == "passed" and law.status == "failed"
        assert economy.price_of(settings, "bread", s) == 200
        assert s.get(WorldState, 1).prices == {"bread": 200}
        _at(ctx, [c], "bakery")
        assert "Town Hall or the Town Square" in _do(ctx, c, ActionName.PROPOSE_LAW, text="Anything at all")
