"""The tick loop. One process, one world.

Per tick:
  1. sleep/wake and travel progress (no LLM)
  2. perceive -> decide (concurrent LLM calls, one per awake idle agent)
  3. validate -> execute (seeded random order; validation re-checked at execution)
  4. needs decay and consequences (collapse, death)
  5. daily world rules (tier re-evaluation)
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from asapville.agents.decide import DecideOutcome, decide
from asapville.agents.persona import persona_block
from asapville.agents.plan import plan_day
from asapville.agents.schemas import ActionName, Decision, Personality
from asapville.angels import fixer, judge, watcher
from asapville.config import Settings
from asapville.db.models import Agent, Anomaly, Event, Location, WorldState
from asapville.llm.budget import NORMAL, PAUSED, SLOW
from asapville.llm.client import LLMGateway
from asapville.llm.routing import WORKER, effort_for, model_for, tier_for
from asapville.world import actions, democracy, economy
from asapville.world.actions import ActionResult, TickContext, arrive
from asapville.world.clock import Clock, in_sleep_window
from asapville.world.events import EventBus, emit, event_payload, remember
from asapville.world.needs import NeedsState, tick_needs
from asapville.world.perception import build_perception, todays_plan
from asapville.world.timeline import record_tick, tick_payload

log = logging.getLogger("asapville.engine")

WAKE_ENERGY = 70.0
REFLEX_HUNGER = 60.0  # without an LLM turn, a body this hungry eats what it carries
REFLEX_ENERGY = 15.0
# Actions an agent keeps doing on its own when it doesn't get an LLM turn (slow mode / capped).
CONTINUABLE = {ActionName.REST, ActionName.FORAGE, ActionName.WORK, ActionName.IDLE}


@dataclass
class TickReport:
    tick: int
    time_label: str = ""
    paused: bool = False
    mode: str = NORMAL
    llm_calls: int = 0
    decisions: int = 0
    hesitations: int = 0
    invalid_actions: int = 0
    deaths: list[str] = field(default_factory=list)
    events: int = 0
    anomalies: int = 0
    interventions: int = 0
    improvised: int = 0


def get_world_state(session: Session) -> WorldState:
    ws = session.get(WorldState, 1)
    if ws is None:
        raise RuntimeError("world not initialised; run `asapville init-world` first")
    return ws


class World:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session],
                 gateway: LLMGateway, bus: EventBus | None = None) -> None:
        self.settings = settings
        self.sessions = session_factory
        self.gateway = gateway
        self.bus = bus or EventBus()
        self.clock = Clock(settings.clock)
        self._personas: dict[int, str] = {}
        with self.sessions() as s:
            gateway.guard.load(s)
            ws = s.get(WorldState, 1)
            if ws is not None:
                from asapville.world.map import seed_locations

                if seed_locations(s, settings.map):  # places added to config since the world began
                    s.commit()
            if ws is not None and economy.open_economy(s, settings, tick=ws.tick):
                s.commit()

    # ------------------------------------------------------------------ helpers
    def persona_for(self, agent: Agent) -> str:
        if agent.id not in self._personas:  # immutable after birth -> cache (stable prompt bytes)
            p = Personality.model_validate(agent.personality)
            self._personas[agent.id] = persona_block(agent.name, p, age=agent.seed_card.get("age"))
        return self._personas[agent.id]

    def _gets_llm_turn(self, agent: Agent, tick: int, mode: str) -> bool:
        if self.gateway.guard.agent_capped(agent.id):
            return False
        if mode == SLOW and agent.tier == WORKER:
            n = max(1, self.settings.budget.slow_mode.worker_decide_every)
            return (tick + agent.id) % n == 0
        return True

    def _food_for_sale_here(self, ctx: TickContext, agent: Agent) -> str | None:
        """The most filling food the town shop where the agent stands sells, has and they can pay for."""
        cfg = self.settings
        here = ctx.locations.get(agent.location_id) if agent.location_id else None
        if here is None:
            return None
        best: tuple[float, str] | None = None
        for item in cfg.economy.shops.get(here.key, []):
            spec = cfg.food.get(item)
            price = economy.price_of(cfg, item, ctx.session)
            if spec is None or spec.nutrition <= 0 or price is None or price > agent.money_cents:
                continue
            if item not in cfg.economy.unlimited and economy.stock_qty(ctx.session, here.id, item) <= 0:
                continue
            if best is None or spec.nutrition > best[0]:
                best = (spec.nutrition, item)
        return best[1] if best else None

    def _continue(self, agent: Agent, ctx: TickContext | None = None) -> Decision:
        """Reflex, then habit, when there is no LLM turn: a hungry body eats what it carries (or
        buys food where it stands), an exhausted one rests; otherwise keep doing a continuable
        action, or idle. No LLM call."""
        food = self.settings.food
        carried = [(food[k].nutrition, k) for k, v in (agent.inventory or {}).items()
                   if v > 0 and k in food and food[k].nutrition > 0]
        if agent.hunger >= REFLEX_HUNGER and carried:
            item = max(carried)[1]
            return Decision(thought="(hunger takes over)", action=ActionName.EAT,
                            action_args={"item": item}, mood=agent.mood_label)
        if agent.hunger >= REFLEX_HUNGER and ctx is not None:
            item = self._food_for_sale_here(ctx, agent)
            if item is not None:
                return Decision(thought="(hunger takes over)", action=ActionName.BUY,
                                action_args={"item": item}, mood=agent.mood_label)
        if agent.energy <= REFLEX_ENERGY:
            return Decision(thought="(too tired to think)", action=ActionName.REST, mood=agent.mood_label)
        try:
            last = ActionName(agent.last_action) if agent.last_action else ActionName.IDLE
        except ValueError:
            last = ActionName.IDLE
        if last not in CONTINUABLE:
            last = ActionName.IDLE
        return Decision(thought="(carries on)", action=last,
                        action_args=dict(agent.last_action_args or {}), mood=agent.mood_label)

    # ------------------------------------------------------------------ phases
    def _sleep_and_travel(self, ctx: TickContext, agents: list[Agent]) -> None:
        cfg = self.settings
        for a in agents:
            if a.travel_ticks_left > 0:
                a.asleep = False
                a.travel_ticks_left -= 1
                if a.travel_ticks_left > 0:
                    continue
                arrive(ctx, a)  # arrived: may now fall asleep below
            in_window = in_sleep_window(ctx.time.hour, a.bedtime_hour, cfg.sleep.hours,
                                        cfg.clock.ticks_per_day)
            if in_window and not a.asleep and a.location_id is not None:
                a.asleep = True
                where = "in my own bed" if a.location_id == a.home_id else "wherever I could, without a bed"
                emit(ctx.session, tick=ctx.tick, kind="sleep", actor=a, location_id=a.location_id,
                     summary=f"{a.name} fell asleep", actor_memory=f"Night came; I slept {where}.",
                     witness_memory="{actor} fell asleep.", importance=1)
            elif a.asleep and not in_window and a.energy >= WAKE_ENERGY:
                a.asleep = False
                emit(ctx.session, tick=ctx.tick, kind="wake", actor=a, location_id=a.location_id,
                     summary=f"{a.name} woke up", actor_memory="I woke up.", importance=1)

    async def _plan_mornings(self, ctx: TickContext, deciders: list[Agent], mode: str) -> int:
        """Each citizen's first waking turn of a day starts with their own plan for it."""
        if 0 <= ctx.time.hour < 5:
            return 0  # plans are made in the morning
        todo = [a for a in deciders if todays_plan(ctx, a) is None and self._gets_llm_turn(a, ctx.tick, mode)]
        if not todo:
            return 0
        cfg = self.settings.llm

        async def one(a: Agent):
            return a, await plan_day(
                gateway=self.gateway, agent_id=a.id, tier=a.tier, model=model_for(a, cfg.models),
                effort=effort_for(a, cfg.effort), persona=self.persona_for(a),
                perception=build_perception(ctx, a), tick=ctx.tick, max_tokens=cfg.max_output_tokens)

        made = 0
        for a, reply in await asyncio.gather(*(one(a) for a in todo)):
            if reply is None:
                continue
            made += 1
            remember(ctx.session, a.id, ctx.tick, "plan", reply.plan, importance=5)
            emit(ctx.session, tick=ctx.tick, kind="plan", actor=a, location_id=a.location_id,
                 visibility="private", summary=f"{a.name} plans the day: {reply.plan}",
                 data={"plan": reply.plan, "mood": reply.mood})
        ctx.session.flush()
        return made

    async def _decide_all(self, ctx: TickContext, deciders: list[Agent],
                          mode: str) -> dict[int, DecideOutcome]:
        perceptions = {a.id: build_perception(ctx, a) for a in deciders}
        cfg = self.settings.llm

        async def one(a: Agent) -> tuple[int, DecideOutcome]:
            if not self._gets_llm_turn(a, ctx.tick, mode):
                return a.id, DecideOutcome(self._continue(a, ctx))
            out = await decide(
                gateway=self.gateway, agent_id=a.id, tier=a.tier,
                model=model_for(a, cfg.models), effort=effort_for(a, cfg.effort),
                persona=self.persona_for(a), perception=perceptions[a.id], memories=[],
                tick=ctx.tick, retries=cfg.json_retries, max_tokens=cfg.max_output_tokens,
            )
            return a.id, out

        results = await asyncio.gather(*(one(a) for a in deciders))
        return dict(results)

    def _execute(self, ctx: TickContext, agents: list[Agent], outcomes: dict[int, DecideOutcome],
                 report: TickReport) -> dict[int, ActionResult]:
        results: dict[int, ActionResult] = {}
        order = [a for a in agents if a.id in outcomes]
        ctx.rng.shuffle(order)  # nobody always acts first
        socialised: set[int] = set()
        for a in order:
            out = outcomes[a.id]
            d = out.decision
            if out.hesitated:
                report.hesitations += 1
                if out.errors:  # parse/LLM failure, not a budget block
                    ctx.session.add(Anomaly(
                        tick=ctx.tick, agent_id=a.id, kind="json_parse_failure", severity=2,
                        detail=f"{a.name} hesitated: {out.errors[-1][:500]}",
                        data={"errors": out.errors}, source="engine"))
            else:
                report.decisions += 1
            perceived = {o.id for o in ctx.session.scalars(select(Agent).where(
                Agent.location_id == a.location_id, Agent.alive.is_(True), Agent.id != a.id))}
            error = actions.validate(ctx, a, d, perceived) if a.location_id else "You are on the road."
            a.last_thought = d.thought
            a.mood_label = d.mood[:64]
            if error:
                report.invalid_actions += 1
                emit(ctx.session, tick=ctx.tick, kind="invalid_action", actor=a,
                     location_id=a.location_id, visibility="private",
                     summary=f"{a.name} tried {d.action.value} {d.action_args}: {error}",
                     data={"action": d.action.value, "args": d.action_args, "error": error},
                     actor_memory=f"I tried to {d.action.value} but couldn't: {error}",
                     importance=2)
                a.last_action, a.last_action_args = ActionName.IDLE.value, {}
                results[a.id] = ActionResult(ok=False, note=error)
                continue
            res = actions.execute(ctx, a, d)
            a.last_action, a.last_action_args = d.action.value, dict(d.action_args)
            if res.fell_asleep:
                a.asleep = True
            socialised |= res.socialised
            results[a.id] = res
        for aid in socialised:
            results.setdefault(aid, ActionResult(ok=True)).socialised.add(aid)
        return results

    def _needs(self, ctx: TickContext, agents: list[Agent], results: dict[int, ActionResult],
               report: TickReport) -> None:
        for a in agents:
            res = results.get(a.id, ActionResult(ok=True))
            outcome = tick_needs(
                NeedsState(hunger=a.hunger, energy=a.energy, social=a.social, health=a.health,
                           mood=a.mood),
                self.settings.needs, asleep=a.asleep,
                at_home=a.location_id is not None and a.location_id == a.home_id,
                resting=res.resting, socialised=a.id in res.socialised,
            )
            st = outcome.state
            a.hunger, a.energy, a.social, a.health, a.mood = (
                st.hunger, st.energy, st.social, st.health, st.mood)
            if outcome.collapsed and a.travel_ticks_left == 0:
                a.asleep = True
                emit(ctx.session, tick=ctx.tick, kind="collapse", actor=a, location_id=a.location_id,
                     summary=f"{a.name} collapsed from exhaustion",
                     actor_memory="My legs gave way and everything went dark.",
                     witness_memory="{actor} collapsed from exhaustion.", importance=7)
            if outcome.died:
                a.alive = False
                a.asleep = False
                a.died_tick = ctx.tick
                a.cause_of_death = outcome.cause_of_death
                report.deaths.append(a.name)
                emit(ctx.session, tick=ctx.tick, kind="death", actor=a, location_id=a.location_id,
                     summary=f"{a.name} died ({outcome.cause_of_death})",
                     witness_memory="{actor} collapsed and died in front of me.", importance=9,
                     data={"cause": outcome.cause_of_death})

    def _daily(self, ctx: TickContext, agents: list[Agent]) -> None:
        changed = economy.adjust_wages(ctx.session, self.settings, tick=ctx.tick)
        if changed:
            parts = [f"{job} {new / 100:.2f}/h (was {old / 100:.2f})" for job, (old, new) in changed.items()]
            emit(ctx.session, tick=ctx.tick, kind="wages", visibility="public",
                 summary="New wages: " + ", ".join(parts),
                 data={"changes": {j: [o, n] for j, (o, n) in changed.items()},
                       "text": "The town council sets today's wages: " + "; ".join(parts)
                               + ". Jobs nobody does pay more each day; crowded ones pay less.",
                       "author": "the town council"})
        for a in agents:
            new_tier = tier_for(a.role, a.money_cents, self.settings.tiers)
            if new_tier != a.tier:
                old = a.tier
                a.tier = new_tier
                emit(ctx.session, tick=ctx.tick, kind="tier_change", actor=a, visibility="system",
                     summary=f"{a.name} tier {old} -> {new_tier}",
                     data={"from": old, "to": new_tier})

    # ------------------------------------------------------------------ public
    async def step(self) -> TickReport:
        with self.sessions() as s:
            ws = get_world_state(s)
            report = TickReport(tick=ws.tick)
            if ws.paused:
                report.paused = True
                report.mode = ws.mode
                return report
            mode = self.gateway.guard.mode
            ws.mode = mode
            if mode == PAUSED:
                ws.paused = True
                log.critical("BUDGET CAP REACHED: world paused at tick %s ($%.4f / $%.2f today)",
                             ws.tick, self.gateway.guard.spent_today, self.settings.budget.daily_usd)
                emit(s, tick=ws.tick, kind="budget_paused", visibility="system",
                     summary="Daily budget exhausted; world paused")
                s.commit()
                report.paused, report.mode = True, mode
                return report

            t = self.clock.at(ws.tick)
            report.time_label, report.mode = t.label(), mode
            first_event_id = (s.scalar(select(Event.id).order_by(Event.id.desc()).limit(1)) or 0)
            ctx = TickContext(
                session=s, settings=self.settings, tick=ws.tick, time=t,
                rng=random.Random(f"{ws.seed}:{ws.tick}"),
                locations={loc.id: loc for loc in s.scalars(select(Location))},
                weather=ws.weather,
            )
            agents = list(s.scalars(select(Agent).where(Agent.alive.is_(True)).order_by(Agent.id)))

            self._sleep_and_travel(ctx, agents)
            deciders = [a for a in agents
                        if not a.asleep and a.travel_ticks_left == 0 and a.location_id is not None]
            calls_before = len(self.gateway.pending)
            await self._plan_mornings(ctx, deciders, mode)
            outcomes = await self._decide_all(ctx, deciders, mode)
            report.llm_calls = len(self.gateway.pending) - calls_before
            results = self._execute(ctx, agents, outcomes, report)
            if ctx.improvised:
                report.improvised = len(ctx.improvised)
                await judge.run(ctx, self.gateway, ctx.improvised)
            self._needs(ctx, agents, results, report)
            democracy.close_due(s, ws.tick, self.settings)
            if self.settings.angels.enabled:
                report.anomalies = len(watcher.scan(ctx, agents))
                pending = watcher.open_anomalies(s)
                everyone_asleep = all(a.asleep or not a.alive for a in agents)
                due = ws.tick % max(1, self.settings.angels.fixer_every) == 0
                if pending and due and not everyone_asleep:
                    fix = await fixer.run(ctx, self.gateway, pending, agents)
                    report.interventions = sum(1 for r in fix.applied if r.applied)
                    if fix.note:
                        log.warning("fixer: %s", fix.note)
            if self.clock.is_new_day(ws.tick):
                self._daily(ctx, agents)
            snapshots = record_tick(s, ws.tick, agents)
            economy.record_world_tick(s, ws.tick, ws, ctx.locations)

            ws.tick += 1
            self.gateway.flush(s)
            s.commit()
            new_events = list(s.scalars(select(Event).where(Event.id > first_event_id)
                                        .order_by(Event.id)))
            report.events = len(new_events)
            for e in new_events:
                self.bus.publish({"type": "event", **event_payload(e)})
            self.bus.publish({
                "type": "tick", "tick": report.tick, "label": t.label(), "hour": t.hour,
                "day": t.day, "season": t.season, "weather": ws.weather, "mode": mode,
                "spent_today": round(self.gateway.guard.spent_today, 4),
                "treasury": ws.treasury_cents,
                "agents": [tick_payload(row) for row in snapshots],
            })
            return report

    async def run(self, *, ticks: int | None = None, realtime: bool = True,
                  after_step: Callable[[TickReport], Awaitable[None]] | None = None) -> list[TickReport]:
        reports: list[TickReport] = []
        done = 0
        while ticks is None or done < ticks:
            rep = await self.step()
            reports.append(rep)
            if after_step is not None and not rep.paused:
                await after_step(rep)
            if rep.paused:
                if not realtime:
                    break
                await asyncio.sleep(1.0)  # poll for `asapville resume`
                continue
            done += 1
            if realtime:
                with self.sessions() as s:
                    speed = max(0.01, get_world_state(s).speed)
                mult = self.settings.budget.slow_mode.tick_multiplier if rep.mode == SLOW else 1.0
                await asyncio.sleep(self.settings.clock.tick_seconds * mult / speed)
        return reports
