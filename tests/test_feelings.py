from sqlalchemy import select

from asapville.agents.schemas import ActionName, Decision
from asapville.db.models import Agent, Location, Relationship
from asapville.world import actions, feelings
from asapville.world.actions import TickContext
from asapville.world.events import emit


def _ctx(world, session, tick=0):
    import random

    return TickContext(session=session, settings=world.settings, tick=tick, time=world.clock.at(tick),
                       rng=random.Random(1),
                       locations={loc.id: loc for loc in session.scalars(select(Location))})


def _pair(s):
    a, b = list(s.scalars(select(Agent).order_by(Agent.id)))[:2]
    a.feelings, b.feelings = [], []
    return a, b


async def test_a_gift_brings_gratitude_toward_the_giver_which_fades(world, sessions):
    with sessions() as s:
        ctx = _ctx(world, s)
        a, b = _pair(s)
        b.location_id = a.location_id = ctx.by_key["square"].id
        a.money_cents, a.asleep, b.asleep = 1000, False, False
        pay = Decision(thought="", action=ActionName.GIVE, action_args={"to": b.id, "asaps": 5})
        actions.execute(ctx, a, pay)
        f = b.feelings[0]
        assert f["e"] == "gratitude" and f["to"] == a.id and f["s"] > 25 and a.name in f["why"]
        first, mood = f["s"], b.mood
        feelings.hourly(b, 1)
        assert b.feelings[0]["s"] < first and b.mood > mood  # it fades, and lifts the mood meanwhile
        for _ in range(400):
            feelings.fade(b)
        assert b.feelings == []


async def test_a_death_brings_grief_to_those_who_cared(world, sessions):
    with sessions() as s:
        a, b = _pair(s)
        s.add(Relationship(agent_id=b.id, other_id=a.id, affinity=0.6, trust=0.5, knows_name=True))
        s.flush()
        emit(s, tick=5, kind="death", actor=a, summary=f"{a.name} died", data={"cause": "starvation"})
        grief = [f for f in b.feelings if f["e"] == "grief"]
        assert grief and grief[0]["to"] == a.id and grief[0]["s"] >= 80
        assert any(f["e"] == "fear" for f in b.feelings)


async def test_citizens_name_their_own_feelings_and_see_them(world, sessions):
    from asapville.world import perception

    d = Decision.model_validate({"thought": "x", "action": "idle", "feelings": [
        {"emotion": "envious", "toward": "2", "why": "he owns the bakery", "strength": 300},
        {"emotion": "zzz-unknown", "why": "?"}, "junk", {"emotion": "love", "toward": 99}]})
    assert [f.emotion for f in d.feelings] == ["envious", "zzz-unknown", "love"]
    with sessions() as s:
        ctx = _ctx(world, s, tick=3)
        a, b = _pair(s)
        feelings.from_decision(a, d.feelings, 3, perceived={b.id})
        assert [(f["e"], f["to"]) for f in a.feelings] == [("envy", b.id), ("love", None)]
        assert a.feelings[0]["s"] == 90  # capped: one hour's feeling never starts overwhelming at 100
        lines = perception.build_perception(ctx, a).feelings
    assert lines[0].startswith("envy toward ") and "overwhelming" in lines[0] and "bakery" in lines[0]
