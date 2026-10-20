import random

from sqlalchemy import select

from asapville.agents.schemas import ActionName, Decision
from asapville.db.models import Agent, Event, Location, Proposal, WorldState
from asapville.world import actions, democracy, elections
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


async def test_the_town_elects_a_council_that_decides_and_grants(world, sessions, settings):
    with sessions() as s:
        people = list(s.scalars(select(Agent).order_by(Agent.id)))
        ctx = _ctx(world, s)
        elections.step(s, 0)
        e = elections.open_election(s)
        assert e is not None and "COUNCIL ELECTION" in render_perception(build_perception(ctx, people[0]))
        for a in people[:4]:
            a.location_id, a.destination_id, a.travel_ticks_left = ctx.by_key["town_hall"].id, None, 0
            assert _do(ctx, a, ActionName.STAND, pitch=f"I am {a.name} and I will fix the mill") is None
        assert "already standing" in _do(ctx, people[0], ActionName.STAND, pitch="again, please")
        # everyone votes from wherever they are
        for voter, choice in zip(people, [0, 0, 1, 1, 2], strict=True):
            voter.location_id = ctx.by_key["farm"].id
            assert _do(ctx, voter, ActionName.ELECT, candidate=people[choice].id) is None
        elections.step(s, elections.ELECTION_HOURS)
        seats = [a.id for a in elections.council(s, elections.ELECTION_HOURS)]
        assert seats == [people[0].id, people[1].id, people[2].id]
        tick = elections.ELECTION_HOURS + 1
        ctx = _ctx(world, s, tick=tick)
        assert "Town council:" in render_perception(build_perception(ctx, people[4]))

        # a grant for public works, decided by two councillors at once
        people[3].location_id = ctx.by_key["town_hall"].id
        assert _do(ctx, people[3], ActionName.PROPOSE_LAW, text="mend the town clock",
                   change={"grant": people[3].id, "to": 50}) is None
        p = s.scalar(select(Proposal))
        money = people[3].money_cents
        fund = s.get(WorldState, 1).treasury_cents
        for c in people[:2]:
            assert _do(ctx, c, ActionName.VOTE, proposal=p.id, choice="yes") is None
        democracy.close_due(s, tick, settings)
        assert p.status == "passed" and people[3].money_cents == money + 5000
        assert s.get(WorldState, 1).treasury_cents == fund - 5000
        assert "council 2-0" in s.scalar(select(Event).where(Event.kind == "law")).summary


async def test_an_election_nobody_votes_in_is_held_again(world, sessions):
    with sessions() as s:
        elections.step(s, 0)
        elections.step(s, elections.ELECTION_HOURS)
        assert elections.council(s, elections.ELECTION_HOURS) == []
        elections.step(s, elections.ELECTION_HOURS + 1)
        assert elections.open_election(s) is not None


async def test_setbacks_are_shown_so_they_are_not_repeated(world, sessions):
    with sessions() as s:
        ctx = _ctx(world, s, tick=2)
        a = s.get(Agent, 1)
        assert _do(ctx, a, ActionName.WITHDRAW, asaps=5) is not None
        from asapville.world.events import emit

        emit(s, tick=1, kind="invalid_action", actor=a, summary="x",
             data={"action": "buy", "error": "There is no fish left; someone has to make more."})
        text = render_perception(build_perception(ctx, a))
        assert "What went wrong for you lately" in text and "no fish left" in text
