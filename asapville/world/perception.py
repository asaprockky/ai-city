"""Perception: what one agent can sense right now. Built from its surroundings and
its own memories only (no omniscience)."""

from __future__ import annotations

from sqlalchemy import select

from asapville.agents.schemas import Destination, JobHere, Notice, Perception, SeenAgent, ShopItem
from asapville.db.models import Agent, Event, Memory
from asapville.world import economy
from asapville.world.actions import TickContext, available_usages
from asapville.world.map import travel_ticks
from asapville.world.needs import describe_needs
from asapville.world.social import describe, knows_name, looks_of

_DOING = {
    "rest": "resting", "eat": "eating", "talk": "talking", "forage": "searching for food",
    "idle": "standing around", "move": "just arrived", "sleep": "asleep", "work": "working",
    "buy": "buying something", "sell": "selling something", "apply": "asking for work",
    "write_notice": "writing a notice",
}

RECENT_TICKS = 3


def todays_plan(ctx: TickContext, agent: Agent) -> str | None:
    """The plan this citizen wrote this simulated day, if any."""
    from asapville.world.clock import Clock

    m = ctx.session.scalar(select(Memory).where(Memory.agent_id == agent.id, Memory.kind == "plan")
                           .order_by(Memory.created_at_tick.desc(), Memory.id.desc()).limit(1))
    if m is None or Clock(ctx.settings.clock).at(m.created_at_tick).day != ctx.time.day:
        return None
    return m.content


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
    cfg = ctx.settings
    dests = [
        Destination(key=loc.key, name=loc.name, kind=loc.kind,
                    travel_hours=travel_ticks(here, loc, cfg.map.cells_per_tick, ctx.travel_slowdown),
                    sells=economy.shop_items(cfg, loc.key), jobs=list(economy.jobs_at(cfg, loc.key)))
        for loc in sorted(ctx.locations.values(), key=lambda x: x.id)
        if loc.id != here.id and (loc.kind != "home" or loc.id == agent.home_id)
    ]
    shop = [ShopItem(item=item, price_cents=economy.price_of(cfg, item) or 0,
                     qty=None if item in cfg.economy.unlimited else economy.stock_qty(s, here.id, item))
            for item in economy.shop_items(cfg, here.key)]
    jobs_here = [JobHere(job=name, wage_cents=job.wage_cents, workers=len(economy.workers_in(s, name)))
                 for name, job in economy.jobs_at(cfg, here.key).items()]
    my_job = cfg.economy.jobs.get(agent.role)
    notices = []
    for ev in s.scalars(select(Event).where(Event.kind == "notice",
                                            Event.tick >= ctx.tick - cfg.economy.notice_ticks)
                        .order_by(Event.id.desc()).limit(5)):
        author_id = ev.data.get("author_id")
        author = ev.data.get("author") or "someone"
        if author_id:
            author_agent = s.get(Agent, author_id)
            author = describe(s, agent.id, author_agent) if author_agent else author
        notices.append(Notice(text=ev.data.get("text", ev.summary), author=author,
                              hours_ago=ctx.tick - ev.tick))
    plan = todays_plan(ctx, agent)
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
        notices=notices,
        job=agent.role if my_job else None,
        workplace=ctx.by_key[my_job.workplace].name if my_job else None,
        wage_cents=my_job.wage_cents if my_job else None,
        shop=shop,
        jobs_here=jobs_here,
        plan=plan,
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
    if p.job:
        lines.append(f"Your job: {p.job} at {p.workplace}, {(p.wage_cents or 0) / 100:.2f} asaps per hour "
                     f"worked (choose 'work' while you are there).")
    else:
        lines.append("You have no job.")
    if p.shop:
        goods = ", ".join(f"{x.item} {x.price_cents / 100:.2f} asaps"
                          + (" (always available)" if x.qty is None else f" ({x.qty} left)")
                          for x in p.shop)
        lines.append(f"For sale here: {goods}.")
    if p.jobs_here:
        lines.append("Jobs here: " + "; ".join(
            f"{j.job} ({j.wage_cents / 100:.2f}/h, {j.workers} working)" for j in p.jobs_here)
            + ". Use 'apply' to take one.")
    lines.append("Places you can walk to (key: name, hours on foot):")
    for d in p.destinations:
        extra = []
        if d.sells:
            extra.append("sells " + ", ".join(d.sells))
        if d.jobs:
            extra.append("hires " + ", ".join(d.jobs))
        tail = " — " + "; ".join(extra) if extra else ""
        lines.append(f"  - {d.key}: {d.name} ({d.travel_hours}h){tail}")
    if p.plan:
        lines.append(f"Your own plan for today (you wrote it this morning): {p.plan}")
    if p.hunger < 50 and p.energy > 35 and p.health > 40:
        lines.append("Your body is fine right now: this hour is yours. Follow your plan, chase what "
                     "you want from life, or try something new of your own (improvise). Nobody "
                     "decides for you.")
    if p.notices:
        lines.append("On the town board:")
        lines.extend(f'  - "{n.text}" — {n.author}, {n.hours_ago}h ago' for n in p.notices)
    if p.recent_dialogue:
        lines.append("What was said recently:")
        lines.extend(f"  - {x}" for x in p.recent_dialogue)
    if p.recent_events:
        lines.append("What just happened:")
        lines.extend(f"  - {x}" for x in p.recent_events)
    return "\n".join(lines)

