import random

from sqlalchemy import select

from asapville.agents.schemas import ActionName, Decision
from asapville.db.models import Agent, Location, Memory
from asapville.world import actions
from asapville.world.actions import TickContext
from asapville.world.clock import Clock
from asapville.world.social import knows_name


def ctx_for(s, settings, tick=0):
    return TickContext(session=s, settings=settings, tick=tick,
                       time=Clock(settings.clock).at(tick), rng=random.Random(1),
                       locations={loc.id: loc for loc in s.scalars(select(Location))})


def dec(action, args=None, dialogue=None):
    return Decision(thought="t", action=action, action_args=args or {}, dialogue=dialogue, mood="m")


def put(s, agent, key):
    agent.location_id = s.scalar(select(Location.id).where(Location.key == key))
    agent.travel_ticks_left, agent.asleep = 0, False


async def test_unimplemented_actions_are_rejected(world, sessions, settings):
    with sessions() as s:
        a = s.get(Agent, 1)
        err = actions.validate(ctx_for(s, settings), a, dec(ActionName.STEAL), set())
        assert err and "yet" in err


async def test_eat_requires_food(world, sessions, settings):
    with sessions() as s:
        a = s.get(Agent, 1)
        a.inventory = {}
        ctx = ctx_for(s, settings)
        assert actions.validate(ctx, a, dec(ActionName.EAT, {"item": "bread"}), set())
        assert actions.validate(ctx, a, dec(ActionName.EAT, {"item": "rock"}), set())
        a.inventory, a.hunger = {"bread": 1}, 60.0
        d = dec(ActionName.EAT, {"item": "bread"})
        assert actions.validate(ctx, a, d, set()) is None
        actions.execute(ctx, a, d)
        assert a.inventory == {} and a.hunger == 60.0 - settings.food["bread"].nutrition


async def test_move_takes_travel_time_and_arrives(world, sessions, settings):
    with sessions() as s:
        a = s.get(Agent, 1)
        put(s, a, "square")
        ctx = ctx_for(s, settings)
        assert actions.validate(ctx, a, dec(ActionName.MOVE, {"to": "atlantis"}), set())
        assert actions.validate(ctx, a, dec(ActionName.MOVE, {"to": "square"}), set())
        assert actions.validate(ctx, a, dec(ActionName.MOVE, {"to": "home_5"}), set())  # not theirs
        actions.execute(ctx, a, dec(ActionName.MOVE, {"to": "forest"}))  # 15 cells / 4 per tick
        assert a.location_id is None and a.travel_ticks_left == 3
        put(s, s.get(Agent, 2), "square")
        actions.execute(ctx, s.get(Agent, 2), dec(ActionName.MOVE, {"to": "market"}))  # 2 cells
        assert s.get(Agent, 2).location_id == ctx.by_key["market"].id


async def test_full_building_turns_agent_away(world, sessions, settings):
    with sessions() as s:
        ctx = ctx_for(s, settings)
        jail = ctx.by_key["jail"]
        jail.capacity = 1
        put(s, s.get(Agent, 2), "jail")
        a = s.get(Agent, 1)
        put(s, a, "tavern")
        actions.execute(ctx, a, dec(ActionName.MOVE, {"to": "jail"}))
        assert a.location_id == ctx.by_key["square"].id


async def test_forage_only_in_the_wild(world, sessions, settings):
    with sessions() as s:
        a = s.get(Agent, 1)
        put(s, a, "market")
        ctx = ctx_for(s, settings)
        assert actions.validate(ctx, a, dec(ActionName.FORAGE), set())
        put(s, a, "forest")
        assert actions.validate(ctx, a, dec(ActionName.FORAGE), set()) is None
        e0 = a.energy
        actions.execute(ctx, a, dec(ActionName.FORAGE))
        assert a.energy == e0 - settings.forage["forest"].energy_cost


async def test_talk_needs_a_present_awake_target_and_introduces(world, sessions, settings):
    with sessions() as s:
        a, b, c = s.get(Agent, 1), s.get(Agent, 2), s.get(Agent, 3)
        for x in (a, b, c):
            put(s, x, "tavern")
        ctx = ctx_for(s, settings)
        d = dec(ActionName.TALK, {"to": b.id}, "Nice weather.")
        assert actions.validate(ctx, a, dec(ActionName.TALK, {"to": 99}, "hi"), {b.id})
        assert actions.validate(ctx, a, dec(ActionName.TALK, {"to": b.id}), {b.id})  # silent
        b.asleep = True
        assert actions.validate(ctx, a, d, {b.id}) == "They're asleep."
        b.asleep = False
        assert not knows_name(s, b.id, a.id)
        assert actions.validate(ctx, a, d, {b.id, c.id}) is None
        res = actions.execute(ctx, a, d)
        assert res.socialised == {a.id, b.id}
        assert knows_name(s, a.id, b.id) and knows_name(s, b.id, a.id)
        assert not knows_name(s, c.id, a.id)  # overhearing is not an introduction
        heard = s.scalars(select(Memory).where(Memory.agent_id == b.id,
                                               Memory.kind == "dialogue")).one()
        assert "a stranger" in heard.content and a.name not in heard.content
        witnessed = s.scalars(select(Memory).where(Memory.agent_id == c.id)).all()
        assert any("Nice weather." in m.content and a.name not in m.content for m in witnessed)
