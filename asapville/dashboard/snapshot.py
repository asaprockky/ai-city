"""Observer-side JSON for the dashboard: the whole town, its timeline and its ledger.

Everything here is omniscient (names, thoughts, needs): it is for the people
watching, never for citizens. The same dict feeds the live page (`/api/bootstrap`)
and the self-contained export (`asapville export`).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from asapville.agents.persona import unpleasant_reasons
from asapville.agents.schemas import Personality
from asapville.config import Settings
from asapville.db.models import Agent, AgentTick, Event, LLMCall, Location, WorldState
from asapville.engine import get_world_state
from asapville.llm.routing import TIER_NAMES, model_for
from asapville.world.clock import Clock
from asapville.world.events import event_payload
from asapville.world.timeline import tick_payload

# Events the simulation layer writes for itself (budget, tiers); the feed hides them.
HIDDEN_VISIBILITY = ("system",)


def location_payload(loc: Location) -> dict[str, Any]:
    return {"id": loc.id, "key": loc.key, "name": loc.name, "kind": loc.kind, "x": loc.x,
            "y": loc.y, "capacity": loc.capacity, "owner_id": loc.owner_id}


def agent_payload(a: Agent, settings: Settings) -> dict[str, Any]:
    p = Personality.model_validate(a.personality)
    seed = a.seed_card or {}
    return {
        "id": a.id, "name": a.name, "tier": a.tier, "tier_name": TIER_NAMES.get(a.tier, str(a.tier)),
        "model": model_for(a, settings.llm.models), "role": a.role,
        "alignment": p.moral_alignment, "unpleasant": unpleasant_reasons(p),
        "age": seed.get("age"), "birthplace": seed.get("birthplace"),
        "wealth_bracket": seed.get("wealth_bracket"), "home_id": a.home_id,
        "bedtime": a.bedtime_hour, "born_tick": a.born_tick, "died_tick": a.died_tick,
        "cause_of_death": a.cause_of_death, "mac": a.virtual_mac, "ip": a.virtual_ip,
        "appearance": p.appearance.model_dump(), "speech_style": p.speech_style,
        "values": p.values, "fears": p.fears, "ambitions": p.ambitions, "quirks": p.quirks,
        "secrets": p.secrets, "backstory": p.backstory,
        "big_five": p.big_five.model_dump(), "dark_traits": p.dark_traits.model_dump(),
    }


def _frame_from_agents(agents: list[Agent], tick: int) -> list[dict[str, Any]]:
    """A snapshot built straight from agent rows (used before the first tick has run)."""
    rows = [AgentTick(tick=tick, agent_id=a.id, location_id=a.location_id,
                      destination_id=a.destination_id, travel_ticks_left=a.travel_ticks_left,
                      asleep=a.asleep, alive=a.alive, hunger=a.hunger, energy=a.energy,
                      social=a.social, health=a.health, mood=a.mood, mood_label=a.mood_label,
                      money_cents=a.money_cents, inventory=dict(a.inventory or {}),
                      action=a.last_action, thought=a.last_thought) for a in agents]
    return [tick_payload(r) for r in rows]


def frames(session: Session, settings: Settings, *, since_tick: int = -1) -> list[dict[str, Any]]:
    """One frame per tick > since_tick: the clock plus every agent's snapshot."""
    clock = Clock(settings.clock)
    rows = session.scalars(select(AgentTick).where(AgentTick.tick > since_tick)
                           .order_by(AgentTick.tick, AgentTick.agent_id))
    out: list[dict[str, Any]] = []
    for r in rows:
        if not out or out[-1]["tick"] != r.tick:
            t = clock.at(r.tick)
            out.append({"tick": r.tick, "label": t.label(), "hour": t.hour, "day": t.day,
                        "season": t.season, "part_of_day": t.part_of_day, "agents": []})
        out[-1]["agents"].append(tick_payload(r))
    return out


def events(session: Session, *, after_id: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    q = (select(Event).where(Event.id > after_id, Event.visibility.not_in(HIDDEN_VISIBILITY))
         .order_by(Event.id))
    if limit:
        q = q.limit(limit)
    return [event_payload(e) for e in session.scalars(q)]


def cost(session: Session) -> dict[str, Any]:
    rows = session.execute(
        select(LLMCall.tier, LLMCall.model, func.count(LLMCall.id), func.sum(LLMCall.cost_usd),
               func.sum(LLMCall.input_tokens), func.sum(LLMCall.output_tokens),
               func.sum(LLMCall.cache_read_tokens))
        .group_by(LLMCall.tier, LLMCall.model).order_by(LLMCall.tier)).all()
    by_model = [{"tier": TIER_NAMES.get(tier, str(tier)), "model": model, "calls": n,
                 "usd": round(usd or 0.0, 4), "input": i or 0, "output": o or 0, "cache_read": cr or 0}
                for tier, model, n, usd, i, o, cr in rows]
    return {"total_usd": round(sum(r["usd"] for r in by_model), 4),
            "calls": sum(r["calls"] for r in by_model), "by_model": by_model}


def world_payload(ws: WorldState, settings: Settings) -> dict[str, Any]:
    t = Clock(settings.clock).at(ws.tick)
    return {"name": settings.world.name, "tick": ws.tick, "paused": ws.paused, "mode": ws.mode,
            "speed": ws.speed, "weather": ws.weather, "label": t.label(), "hour": t.hour,
            "day": t.day, "season": t.season, "ticks_per_day": settings.clock.ticks_per_day,
            "tick_seconds": settings.clock.tick_seconds,
            "budget_daily_usd": settings.budget.daily_usd}


def bootstrap(session: Session, settings: Settings) -> dict[str, Any]:
    ws = get_world_state(session)
    agents = list(session.scalars(select(Agent).order_by(Agent.id)))
    timeline = frames(session, settings)
    if not timeline:  # nothing has happened yet: show everyone where they were born
        t = Clock(settings.clock).at(ws.tick)
        timeline = [{"tick": ws.tick, "label": t.label(), "hour": t.hour, "day": t.day,
                     "season": t.season, "part_of_day": t.part_of_day,
                     "agents": _frame_from_agents(agents, ws.tick)}]
    return {
        "world": world_payload(ws, settings),
        "map": {"width": settings.map.width, "height": settings.map.height,
                "cells_per_tick": settings.map.cells_per_tick},
        "locations": [location_payload(loc)
                      for loc in session.scalars(select(Location).order_by(Location.id))],
        "agents": [agent_payload(a, settings) for a in agents],
        "frames": timeline,
        "events": events(session),
        "cost": cost(session),
    }
