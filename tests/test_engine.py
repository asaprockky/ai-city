import json

from sqlalchemy import func, select

from asapville.cli import init_world
from asapville.db.models import Agent, Event, LLMCall, Memory, WorldState
from asapville.engine import World
from asapville.llm.budget import BudgetGuard
from asapville.llm.client import LLMGateway
from asapville.llm.fake import FakeLLM
from asapville.world.clock import Clock, in_sleep_window


async def test_five_agents_live_two_days_for_free(world, sessions, settings):
    await world.run(ticks=48, realtime=False)
    with sessions() as s:
        assert s.get(WorldState, 1).tick == 48
        agents = list(s.scalars(select(Agent)))
        assert len(agents) == 5
        moves = s.scalar(select(func.count(Event.id)).where(Event.kind == "arrive"))
        assert moves >= 5
        assert s.scalar(select(func.sum(LLMCall.cost_usd))) == 0
        assert s.scalar(select(func.count(LLMCall.id))) > 50


async def test_sleeping_agents_make_no_llm_calls(world, sessions, settings):
    await world.run(ticks=48, realtime=False)
    clock = Clock(settings.clock)
    with sessions() as s:
        bedtimes = {a.id: a.bedtime_hour for a in s.scalars(select(Agent))}
        for call in s.scalars(select(LLMCall).where(LLMCall.purpose == "decide")):
            hour = clock.at(call.tick).hour
            # never called during the first hours of the sleep window (energy is low then)
            offset = (hour - bedtimes[call.agent_id]) % 24
            assert not (offset < 4 and in_sleep_window(hour, bedtimes[call.agent_id], 7)), (
                f"agent {call.agent_id} called the LLM at {hour}:00 while asleep")
        night_calls = s.scalar(select(func.count(LLMCall.id)).where(
            LLMCall.purpose == "decide", LLMCall.tick.between(18, 23)))  # 00:00-05:00 day 2
        day_calls = s.scalar(select(func.count(LLMCall.id)).where(
            LLMCall.purpose == "decide", LLMCall.tick.between(4, 9)))  # 10:00-15:00 day 1
        assert night_calls < day_calls / 2


async def test_same_seed_same_world(settings, tmp_path):
    from asapville.db.session import make_engine, make_session_factory

    def summaries(db_name):
        settings.db_url = f"sqlite:///{tmp_path / db_name}"
        sessions = make_session_factory(make_engine(settings.db_url))
        guard = BudgetGuard(settings.budget, today=lambda: "2026-10-20")
        gw = LLMGateway(FakeLLM(), settings.llm, guard)
        return settings.db_url, sessions, gw

    out = []
    for name in ("a.db", "b.db"):
        _, sessions, gw = summaries(name)
        await init_world(settings, sessions=sessions, gateway=gw)
        await World(settings, sessions, gw).run(ticks=30, realtime=False)
        with sessions() as s:
            out.append([e.summary for e in s.scalars(select(Event).order_by(Event.id))])
    assert out[0] == out[1]


async def test_starving_agent_dies_without_scripting(world, sessions, fake):
    idle = json.dumps({"thought": "I won't move.", "action": "idle", "action_args": {},
                       "dialogue": None, "mood": "stubborn"})
    fake.handlers["decide"] = lambda req, rng: idle
    with sessions() as s:
        a = s.get(Agent, 1)
        a.inventory, a.hunger, a.health = {}, 95.0, 12.0
        s.commit()
    await world.run(ticks=10, realtime=False)
    with sessions() as s:
        a = s.get(Agent, 1)
        assert not a.alive and a.cause_of_death == "starvation"
        assert s.scalar(select(Event).where(Event.kind == "death")) is not None


async def test_budget_slow_mode_then_pause(settings, sessions):
    settings.budget.daily_usd = 0.01  # small enough to hit before anyone can starve
    guard = BudgetGuard(settings.budget, today=lambda: "2026-10-20")
    gw = LLMGateway(FakeLLM(billable=True), settings.llm, guard)
    await init_world(settings, sessions=sessions, gateway=gw)
    world = World(settings, sessions, gw)
    reports = await world.run(ticks=200, realtime=False)
    modes = [r.mode for r in reports]
    assert "slow" in modes and reports[-1].paused and reports[-1].mode == "budget_paused"
    slow = [r for r in reports if r.mode == "slow" and not r.paused]
    normal = [r for r in reports if r.mode == "normal" and r.time_label.endswith(("10:00", "11:00"))]
    if slow and normal:  # workers mostly skip their LLM turn in slow mode
        assert min(r.llm_calls for r in slow) < max(r.llm_calls for r in normal)
    with sessions() as s:
        ws = s.get(WorldState, 1)
        assert ws.paused and ws.mode == "budget_paused"
        spent = s.scalar(select(func.sum(LLMCall.cost_usd)))
        # the cap is hard: at most one tick's worth of in-flight calls past it
        assert spent < settings.budget.daily_usd * 1.5


async def test_worker_promotion_changes_model(world, sessions, fake):
    with sessions() as s:
        a = s.scalars(select(Agent).where(Agent.tier == 3)).first()
        aid = a.id
        a.money_cents = 200_000  # suddenly rich -> manager tier at the next daily review
        s.commit()
    await world.run(ticks=34, realtime=False)  # crosses midnight, then a morning awake
    with sessions() as s:
        assert s.get(Agent, aid).tier == 2
        models_after = {c.model for c in s.scalars(select(LLMCall).where(
            LLMCall.agent_id == aid, LLMCall.tick > 18, LLMCall.purpose == "decide"))}
        models_before = {c.model for c in s.scalars(select(LLMCall).where(
            LLMCall.agent_id == aid, LLMCall.tick <= 18, LLMCall.purpose == "decide"))}
    assert models_before == {"claude-haiku-5-5"}
    assert models_after == {"claude-sonnet-5-5"}


async def test_no_citizen_memory_mentions_angels_or_system(world, sessions):
    await world.run(ticks=48, realtime=False)
    with sessions() as s:
        for m in s.scalars(select(Memory)):
            low = m.content.lower()
            assert "angel" not in low and "tier" not in low and "budget" not in low
        system_events = {e.id for e in s.scalars(select(Event).where(Event.visibility == "system"))}
        assert not s.scalars(select(Memory).where(Memory.source_event_id.in_(system_events))).first()


async def test_init_world_without_key_touches_no_database(settings, tmp_path, monkeypatch):
    import pytest

    monkeypatch.delenv("ASAPVILLE_ANTHROPIC_KEY", raising=False)
    settings.llm.backend = "anthropic"
    db = tmp_path / "nokey.db"
    settings.db_url = f"sqlite:///{db}"
    with pytest.raises(RuntimeError, match="ASAPVILLE_ANTHROPIC_KEY"):
        await init_world(settings)
    assert not db.exists() or db.stat().st_size == 0


async def test_a_capped_citizen_still_eats_what_they_carry(world, sessions, gateway):
    with sessions() as s:
        a = s.get(Agent, 1)
        a.hunger, a.inventory, a.bedtime_hour, a.energy = 95, {"bread": 2}, 22, 80
        s.commit()
    gateway.guard.spent_by_agent[1] = 999.0  # over their daily cap: no LLM turn
    await world.step()
    with sessions() as s:
        a = s.get(Agent, 1)
        assert a.inventory == {"bread": 1} and a.hunger < 95


async def test_a_capped_citizen_with_money_buys_a_meal_where_they_stand(world, sessions, gateway):
    from asapville.db.models import Location

    with sessions() as s:
        a = s.get(Agent, 1)
        tavern = s.scalar(select(Location).where(Location.key == "tavern"))
        a.location_id, a.destination_id, a.travel_ticks_left = tavern.id, None, 0
        a.hunger, a.inventory, a.bedtime_hour, a.energy, a.money_cents = 100, {}, 22, 80, 2000
        s.commit()
    gateway.guard.spent_by_agent[1] = 999.0
    await world.step()
    with sessions() as s:
        a = s.get(Agent, 1)
        assert a.money_cents == 1500 and a.hunger < 100
