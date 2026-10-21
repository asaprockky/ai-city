import random

from sqlalchemy import select

from asapville.agents.schemas import ActionName, Decision
from asapville.db.models import Agent, Event, Location, Memory
from asapville.world import actions
from asapville.world.actions import TickContext
from asapville.world.perception import build_perception, render_perception
from asapville.world.social import get_relationship, reputation


def _ctx(world, session, tick=0, seed=1):
    return TickContext(session=session, settings=world.settings, tick=tick, time=world.clock.at(tick),
                       rng=random.Random(seed),
                       locations={loc.id: loc for loc in session.scalars(select(Location))})


def _at(ctx, agents, key):
    for a in agents:
        a.location_id, a.destination_id, a.travel_ticks_left, a.asleep = ctx.by_key[key].id, None, 0, False


async def test_kindness_and_wrongs_are_remembered(world, sessions):
    with sessions() as s:
        ctx = _ctx(world, s)
        a, b, c = list(s.scalars(select(Agent).order_by(Agent.id)))[:3]
        _at(ctx, [a, b, c], "square")
        a.inventory, b.money_cents = {"bread": 2}, 1000
        here = {a.id, b.id, c.id}
        actions.execute(ctx, a, Decision(thought="", action=ActionName.GIVE,
                                         action_args={"to": b.id, "item": "bread", "qty": 1}))
        rel = get_relationship(s, b.id, a.id)
        assert rel.trust > 0 and rel.affinity > 0 and rel.history[-1]["did"] == "gave me 1 bread"
        hit = Decision(thought="", action=ActionName.ATTACK, action_args={"to": b.id})
        assert actions.validate(ctx, a, hit, here - {a.id}) is None
        health = b.health
        actions.execute(ctx, a, hit)
        assert b.health == health - actions.ATTACK_DAMAGE
        assert get_relationship(s, b.id, a.id).trust < 0  # one blow outweighs one loaf
        assert get_relationship(s, c.id, a.id).history[-1]["did"].startswith("attacked")
        assert reputation(a) < 0
        text = render_perception(build_perception(ctx, b))
        assert "People you know and what they have done to you" in text
        assert "gave me 1 bread; attacked me" in text


async def test_theft_seen_and_unseen(world, sessions):
    with sessions() as s:
        a, b = list(s.scalars(select(Agent).order_by(Agent.id)))[:2]
        b.money_cents = 1000
        steal = Decision(thought="", action=ActionName.STEAL, action_args={"from": b.id, "asaps": 4})
        caught = unseen = 0
        for seed in range(40):
            ctx = _ctx(world, s, seed=seed)
            _at(ctx, [a, b], "market")
            b.asleep = True  # sleepers rarely notice
            b.money_cents = 1000
            actions.execute(ctx, a, steal)
            ev = s.scalars(select(Event).where(Event.kind == "steal").order_by(Event.id.desc())).first()
            if ev.data["noticed"]:
                caught += 1
            else:
                unseen += 1
            assert b.money_cents == 600
        assert unseen > caught > 0
        missing = [m.content for m in s.scalars(select(Memory).where(Memory.agent_id == b.id))]
        assert any("missing from my things" in m for m in missing)
        assert any("caught them at it" in m for m in missing)
        ctx = _ctx(world, s)
        _at(ctx, [a, b], "market")
        nothing = Decision(thought="", action=ActionName.STEAL, action_args={"from": b.id})
        assert "Steal what" in actions.validate(ctx, a, nothing, {b.id})
