from sqlalchemy import select

from asapville.angels import fixer, watcher
from asapville.db.models import Agent, Anomaly, Event, Intervention, LLMCall, Location, WorldState
from asapville.world import economy


async def test_watcher_files_each_problem_once(world, sessions, settings):
    settings.angels.enabled = False
    with sessions() as s:
        import random

        from asapville.world.actions import TickContext

        ws = s.get(WorldState, 1)
        ctx = TickContext(session=s, settings=settings, tick=ws.tick, time=world.clock.at(ws.tick),
                          rng=random.Random(1),
                          locations={loc.id: loc for loc in s.scalars(select(Location))})
        agents = list(s.scalars(select(Agent)))
        first = [a.kind for a in watcher.scan(ctx, agents)]
        assert "no_workers_miller" in first and "no_workers_quarryman" in first
        assert "no_workers_baker" not in first  # 24 loaves on the shelf is plenty
        assert watcher.scan(ctx, agents) == []  # still open -> not filed twice


async def test_watcher_sees_starvation_and_empty_shelves(world, sessions, settings):
    with sessions() as s:
        a = s.scalars(select(Agent).order_by(Agent.id)).first()
        a.hunger, a.health, a.inventory = 95, 15, {}
        for loc in s.scalars(select(Location)):
            for item in ("bread", "vegetables"):
                q = economy.stock_qty(s, loc.id, item)
                if q:
                    economy.stock_add(s, loc.id, item, -q)
        s.commit()
    await world.step()
    with sessions() as s:
        rows = {r.kind: r for r in s.scalars(select(Anomaly).where(Anomaly.source == "watcher"))}
        assert rows["starving"].severity == 4 and rows["starving"].agent_id == a.id
        assert rows["no_food_for_sale"].severity == 4


async def test_fixer_intervenes_within_limits(world, sessions, settings, fake):
    with sessions() as s:
        victim = s.scalars(select(Agent).order_by(Agent.id)).first()
        victim.hunger, victim.health, victim.inventory = 95, 15, {}
        bakery = s.scalar(select(Location).where(Location.key == "bakery"))
        economy.stock_add(s, bakery.id, "bread", -24)
        market = s.scalar(select(Location).where(Location.key == "market"))
        economy.stock_add(s, market.id, "vegetables", -20)
        s.commit()
    rep = await world.step()  # tick 0: watcher files, fixer runs (0 % fixer_every == 0)
    assert rep.anomalies >= 2 and rep.interventions >= 3
    with sessions() as s:
        ivs = list(s.scalars(select(Intervention).order_by(Intervention.id)))
        targets = [i.target for i in ivs if i.applied]
        assert "stock:bakery:bread" in targets and "notice" in targets
        assert f"agent:{victim.id}:inventory" in targets
        assert not any(t.endswith(":role") for t in targets)  # citizens choose their own work
        assert economy.stock_qty(s, bakery.id, "bread") == 20
        assert s.get(Agent, victim.id).inventory.get("bread") == 2
        assert all(a.status == "fixed" for a in s.scalars(select(Anomaly).where(Anomaly.source == "watcher")))
        call = s.scalar(select(LLMCall).where(LLMCall.purpose == "fixer"))
        assert call.angel_role == "fixer" and call.agent_id is None and call.tier == 0
        assert call.model == settings.llm.models.opus  # severity 4 -> Opus, with a reason
        assert call.reason
        angel_event = s.scalar(select(Event).where(Event.kind == "angel"))
        assert angel_event.visibility == "system"
        # citizens saw a cart and a notice, never an angel
        assert s.scalar(select(Event).where(Event.kind == "delivery")) is not None
        assert s.scalar(select(Event).where(Event.kind == "notice",
                                            Event.data["author"].as_string() == "the town council"))


async def test_fixer_rejects_bad_interventions(world, sessions, settings, fake):
    fake.script("fixer", '{"assessment": "x", "interventions": ['
                         '{"kind": "grant", "agent_id": 1, "cents": 999999, "why": "too much"},'
                         '{"kind": "restock", "location": "moon", "item": "bread", "qty": 5, "why": "no"},'
                         '{"kind": "appoint", "agent_id": 1, "job": "wizard", "why": "no"},'
                         '{"kind": "grant", "agent_id": 1, "cents": 500, "why": "ok"}]}')
    with sessions() as s:
        a = s.get(Agent, 1)
        a.hunger, a.health = 95, 15
        money = a.money_cents
        s.commit()
    await world.step()
    with sessions() as s:
        ivs = list(s.scalars(select(Intervention).order_by(Intervention.id)))
        assert [i.applied for i in ivs] == [False, False, False, True]
        assert all(i.reason.startswith("REJECTED") for i in ivs[:3])
        assert s.get(Agent, 1).money_cents == money + 500
        assert s.get(WorldState, 1).treasury_cents == settings.economy.treasury_cents - 500


async def test_fixer_unreadable_reply_keeps_anomalies_open(world, sessions, settings, fake):
    fake.script("fixer", "I refuse to answer in JSON")
    with sessions() as s:
        a = s.get(Agent, 1)
        a.hunger, a.health = 95, 15
        s.commit()
    await world.step()
    with sessions() as s:
        starving = s.scalar(select(Anomaly).where(Anomaly.kind == "starving"))
        assert starving.status == "open" and starving.fix_attempts == 1
        assert s.scalar(select(Anomaly).where(Anomaly.kind == "fixer_parse_failure")) is not None


def test_fixer_prompt_is_static_and_parse_is_strict(settings):
    assert fixer.rules(settings) == fixer.rules(settings)
    assert "max 50" in fixer.rules(settings)
    r = fixer.parse_reply('```json\n{"assessment": "fine", "interventions": []}\n```')
    assert r.assessment == "fine" and r.interventions == []
    try:
        fixer.parse_reply("nope")
    except ValueError as exc:
        assert "no JSON" in str(exc)
    else:
        raise AssertionError("should have failed")
    assert watcher.STUCK_COUNT == 3
