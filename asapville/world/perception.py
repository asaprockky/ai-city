"""Perception: what one agent can sense right now. Built from its surroundings and
its own memories only (no omniscience)."""

from __future__ import annotations

from sqlalchemy import select

from asapville.agents.schemas import Destination, Perception, SeenAgent
from asapville.db.models import Agent, Memory
from asapville.world.actions import TickContext, available_usages
from asapville.world.map import travel_ticks
from asapville.world.needs import describe_needs
from asapville.world.social import knows_name, looks_of

_DOING = {
    "rest": "resting", "eat": "eating", "talk": "talking", "forage": "searching for food",
    "idle": "standing around", "move": "just arrived", "sleep": "asleep", "work": "working",
}

RECENT_TICKS = 3


def build_perception(ctx: TickContext, agent: Agent) -> Perception:
    s = ctx.session
    here = ctx.locations[agent.location_id]  # caller guarantees the agent isn't travelling
    others = s.scalars(select(Agent).where(
        Agent.location_id == here.id, Agent.alive.is_(True), Agent.travel_ticks_left == 0,
        Agent.id != agent.id))
    seen = [
        SeenAgent(
            ref=o.id,
            name=o.name if knows_name(s, agent.id, o.id) else None,
            looks=looks_of(o),
            doing="asleep" if o.asleep else _DOING.get(o.last_action or "", None),
            asleep=o.asleep,
        )
        for o in others
    ]
    dests = [
        Destination(key=loc.key, name=loc.name, kind=loc.kind,
                    travel_hours=travel_ticks(here, loc, ctx.settings.map.cells_per_tick,
                                              ctx.travel_slowdown))
        for loc in sorted(ctx.locations.values(), key=lambda x: x.id)
        if loc.id != here.id and (loc.kind != "home" or loc.id == agent.home_id)
    ]
    recent = list(s.scalars(
        select(Memory).where(Memory.agent_id == agent.id,
                             Memory.created_at_tick >= ctx.tick - RECENT_TICKS)
        .order_by(Memory.created_at_tick, Memory.id)))
    return Perception(
        time_label=ctx.time.label(),
        part_of_day=ctx.time.part_of_day,
        season=ctx.time.season,
        weather=ctx.weather,
        location_key=here.key,
        location_name=here.name,
        location_kind=here.kind,
        is_home=here.id == agent.home_id,
        here=seen,
        destinations=dests,
        body=describe_needs(agent.hunger, agent.energy, agent.social, agent.health),
        hunger=round(agent.hunger),
        energy=round(agent.energy),
        health=round(agent.health),
        social=round(agent.social),
        money_cents=agent.money_cents,
        inventory=dict(agent.inventory or {}),
        recent_dialogue=[m.content for m in recent if m.kind == "dialogue"][-8:],
        recent_events=[m.content for m in recent if m.kind in ("observation", "outcome", "action")][-8:],
        available_actions=available_usages(),
    )


def render_perception(p: Perception) -> str:
    """Perception as second-person prose for the dynamic part of the prompt."""
    lines = [
        f"It is {p.time_label} ({p.part_of_day}). Season: {p.season}. Weather: {p.weather}.",
        f"You are at {p.location_name} [{p.location_key}]" + (" — your home." if p.is_home else "."),
    ]
    if p.here:
        lines.append("People here (use ref to address them):")
        for a in p.here:
            who = a.name or f"a stranger ({a.looks})"
            state = f" — {a.doing}" if a.doing else ""
            lines.append(f"  - ref {a.ref}: {who}{state}")
    else:
        lines.append("Nobody else is here.")
    lines.append(f"Your body: hunger {p.hunger}/100, energy {p.energy}/100, health {p.health}/100, "
                 f"social {p.social}/100." + (" " + "; ".join(p.body).capitalize() + "." if p.body else ""))
    asaps = p.money_cents / 100
    inv = ", ".join(f"{k} x{v}" for k, v in sorted(p.inventory.items())) or "nothing"
    lines.append(f"You carry {asaps:.2f} asaps and: {inv}.")
    lines.append("Places you can walk to (key: name, hours on foot):")
    lines.extend(f"  - {d.key}: {d.name} ({d.travel_hours}h)" for d in p.destinations)
    if p.notices:
        lines.append("Notices you can see: " + " | ".join(p.notices))
    if p.recent_dialogue:
        lines.append("What was said recently:")
        lines.extend(f"  - {x}" for x in p.recent_dialogue)
    if p.recent_events:
        lines.append("What just happened:")
        lines.extend(f"  - {x}" for x in p.recent_events)
    return "\n".join(lines)

