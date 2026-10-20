import json

from sqlalchemy import select

from asapville.agents.schemas import ActionName, Decision
from asapville.angels import judge
from asapville.db.models import Agent, Anomaly, Event, Location, WorldState
from asapville.world import actions
from asapville.world.actions import TickContext


def _ctx(world, session, tick=0):
    import random

    return TickContext(session=session, settings=world.settings, tick=tick, time=world.clock.at(tick),
                       rng=random.Random(1),
                       locations={loc.id: loc for loc in session.scalars(select(Location))})


def _two_at(ctx, s, key):
    a, b = list(s.scalars(select(Agent).order_by(Agent.id)))[:2]
    for x in (a, b):
        x.location_id, x.destination_id, x.travel_ticks_left, x.asleep = ctx.by_key[key].id, None, 0, False
    s.flush()
    return a, b


async def test_give_goods_and_money(world, sessions):
    with sessions() as s:
        ctx = _ctx(world, s)
        a, b = _two_at(ctx, s, "square")
        a.inventory, a.money_cents, b.inventory, b.money_cents = {"berries": 3}, 500, {}, 0
        here = {b.id}
        d = Decision(thought="", action=ActionName.GIVE,
                     action_args={"to": b.id, "item": "berries", "qty": 2})
        assert actions.validate(ctx, a, d, here) is None
        actions.execute(ctx, a, d)
        assert a.inventory == {"berries": 1} and b.inventory == {"berries": 2}
        cash = Decision(thought="", action=ActionName.GIVE, action_args={"to": b.id, "asaps": 2.5})
        actions.execute(ctx, a, cash)
        assert a.money_cents == 250 and b.money_cents == 250
        too_much = Decision(thought="", action=ActionName.GIVE, action_args={"to": b.id, "asaps": 99})
        assert "only have" in actions.validate(ctx, a, too_much, here)
        b.asleep = True
        assert "asleep" in actions.validate(ctx, a, d, here)
        ev = s.scalar(select(Event).where(Event.kind == "give"))
        assert ev.target_id == b.id and ev.data == {"item": "berries", "qty": 2}


async def test_improvise_is_judged_within_bounds(world, sessions, fake):
    fake.script("judge", json.dumps({"results": [{
        "agent_id": 1, "success": True,
        "outcome": "{actor} cooks a fragrant stew over a small fire.",
        "memory": "I cooked a stew from my fish and vegetables.",
        "energy_cost": 99, "consume": {"fish": 1, "vegetables": 1, "bread": 5},
        "produce": {"stew": 1, "bread": 9, "asaps": 100, "Golden Crown!!": 1},
        "mood": 50, "health": 50, "notice": "Stew tonight at the square, bring a bowl"}]}))
    with sessions() as s:
        ctx = _ctx(world, s)
        a = s.get(Agent, 1)
        a.location_id, a.destination_id, a.travel_ticks_left = ctx.by_key["square"].id, None, 0
        a.inventory, a.energy, a.health, a.mood = {"fish": 1, "vegetables": 1, "bread": 1}, 80, 50, 50
        d = Decision(thought="", action=ActionName.IMPROVISE,
                     action_args={"what": "cook a stew", "uses": {"fish": 1, "vegetables": 1}})
        assert actions.validate(ctx, a, d, set()) is None
        actions.execute(ctx, a, d)
        assert ctx.improvised and ctx.improvised[0]["uses"] == {"fish": 1, "vegetables": 1}
        await judge.run(ctx, world.gateway, ctx.improvised)
        # bread wasn't in "uses" so it isn't consumed; stew is the tavern's unlimited good (never made);
        # the 9 loaves asked for are capped by nourishment: (30 + 20) * 1.25 = 62 -> one 35-point loaf
        assert a.inventory["bread"] == 2 and "fish" not in a.inventory
        assert a.inventory.get("stew") is None
        assert a.inventory.get("golden crown") == 1 and "asaps" not in a.inventory
        assert a.energy == 60 and a.health == 55 and a.mood == 60  # clamped to 20 / +5 / +10
        ev = s.scalar(select(Event).where(Event.kind == "improvise"))
        assert ev.summary.startswith(a.name) and ev.data["success"] is True
        assert s.scalar(select(Event).where(Event.kind == "notice", Event.agent_id == 1)) is not None
        bad = Decision(thought="", action=ActionName.IMPROVISE,
                       action_args={"what": "x", "uses": {"gold": 1}})
        assert "don't have" in actions.validate(ctx, a, bad, set())


async def test_unreadable_judge_means_nothing_much_happens(world, sessions, fake):
    fake.script("judge", "the hour passes")
    with sessions() as s:
        ctx = _ctx(world, s)
        a = s.get(Agent, 1)
        a.inventory = {"berries": 2}
        ctx.improvised.append({"agent_id": 1, "what": "build a cathedral", "uses": {"berries": 2},
                               "with_id": None, "location_id": a.location_id})
        await judge.run(ctx, world.gateway, ctx.improvised)
        assert a.inventory == {"berries": 2}
        ev = s.scalar(select(Event).where(Event.kind == "improvise"))
        assert "nothing much" in ev.summary and ev.data["success"] is False


async def test_clinic_treats_for_money(world, sessions, settings):
    with sessions() as s:
        ctx = _ctx(world, s)
        a = s.get(Agent, 1)
        a.location_id, a.destination_id, a.travel_ticks_left = ctx.by_key["clinic"].id, None, 0
        a.health, a.money_cents = 20, 1000
        d = Decision(thought="", action=ActionName.BUY, action_args={"item": "treatment"})
        assert actions.validate(ctx, a, d, set()) is None
        actions.execute(ctx, a, d)
        assert a.health == 50 and a.money_cents == 200 and "treatment" not in (a.inventory or {})


async def test_watcher_sees_failing_health_poverty_and_a_stalled_economy(world, sessions):
    with sessions() as s:
        a, b = list(s.scalars(select(Agent).order_by(Agent.id)))[:2]
        a.health, a.hunger = 15, 10
        b.hunger, b.money_cents, b.inventory = 60, 10, {}
        s.get(WorldState, 1).tick = 20
        s.commit()
    await world.step()
    with sessions() as s:
        kinds = {(x.kind, x.agent_id) for x in s.scalars(select(Anomaly).where(Anomaly.source == "watcher"))}
        assert ("failing_health", a.id) in kinds and ("broke_and_hungry", b.id) in kinds
        assert ("production_stalled", None) in kinds


async def test_fake_town_improvises_gives_and_writes_live_bundles(world, sessions, settings, tmp_path):
    from asapville.cli import _live_writer

    await world.run(ticks=24, realtime=False, after_step=_live_writer(settings, sessions, str(tmp_path)))
    files = sorted(p.name for p in tmp_path.glob("t*.json"))
    assert files[0] == "t000000.json" and len(files) == 24
    b = json.loads((tmp_path / "t000005.json").read_text())
    assert b["tick"] == 5 and b["frame"]["tick"] == 5 and len(b["frame"]["agents"]) == 5
    assert {"world", "cost", "economy", "angels", "events"} <= set(b)
    with sessions() as s:
        kinds = {k for (k,) in s.execute(select(Event.kind).distinct())}
    assert "improvise" in kinds
