"""The Watcher: a $0, code-based scan for trouble, run every tick.

It files `Anomaly` rows (one open row per kind+subject) and never touches the
world itself; the Fixer decides what, if anything, to do about them.
"""

from __future__ import annotations

from sqlalchemy import func, select

from asapville.db.models import Agent, Anomaly, Event
from asapville.world import economy
from asapville.world.actions import TickContext

STUCK_WINDOW = 6  # ticks
STUCK_COUNT = 3  # turned away / invalid actions inside the window
LOW_STOCK = 8  # units of a job's product below which a missing worker matters
FAILING_HEALTH = 25  # below this a citizen is in danger even when fed
STALL_WINDOW = 12  # ticks without one productive hour of work
COOLDOWN = 12  # a handled problem isn't filed again for this many ticks (deaths never repeat)


def open_anomalies(session) -> list[Anomaly]:
    return list(session.scalars(select(Anomaly).where(Anomaly.status == "open")
                                .order_by(Anomaly.severity.desc(), Anomaly.id)))


def scan(ctx: TickContext, agents: list[Agent]) -> list[Anomaly]:
    s = ctx.session
    cfg = ctx.settings
    already = {(a.kind, a.agent_id) for a in open_anomalies(s)}
    already |= {(a.kind, a.agent_id) for a in s.scalars(
        select(Anomaly).where(Anomaly.source == "watcher", Anomaly.tick > ctx.tick - COOLDOWN))}
    already |= {("death", a.agent_id) for a in s.scalars(select(Anomaly).where(Anomaly.kind == "death"))}
    found: list[Anomaly] = []

    def add(kind: str, severity: int, detail: str, agent: Agent | None = None,
            data: dict | None = None) -> None:
        key = (kind, agent.id if agent else None)
        if key in already:
            return
        already.add(key)
        found.append(Anomaly(tick=ctx.tick, agent_id=agent.id if agent else None, kind=kind,
                             severity=severity, detail=detail, data=data or {}, source="watcher"))

    for a in agents:
        if not a.alive:
            if a.died_tick == ctx.tick:
                add("death", 5, f"{a.name} died: {a.cause_of_death}", a,
                    {"cause": a.cause_of_death, "money_cents": a.money_cents})
            continue
        if a.hunger >= cfg.needs.starving_threshold and a.health < 50:
            inv = ", ".join(f"{k} x{v}" for k, v in (a.inventory or {}).items()) or "nothing"
            where = ctx.locations[a.location_id].name if a.location_id else "on the road"
            add("starving", 4 if a.health < 20 else 3,
                f"{a.name} is starving (hunger {a.hunger:.0f}, health {a.health:.0f}) at {where} "
                f"with {a.money_cents / 100:.2f} asaps, carrying {inv}; job: {a.role}", a,
                {"hunger": a.hunger, "health": a.health, "money_cents": a.money_cents})
        elif a.health < FAILING_HEALTH:
            where = ctx.locations[a.location_id].name if a.location_id else "on the road"
            add("failing_health", 4 if a.health < 12 else 3,
                f"{a.name}'s health is {a.health:.0f} (hunger {a.hunger:.0f}) at {where}, "
                f"{a.money_cents / 100:.2f} asaps; doing: {a.last_action}", a,
                {"health": a.health, "money_cents": a.money_cents})
        cheapest = min((p for item, p in cfg.economy.prices.items()
                        if item in cfg.food and cfg.food[item].nutrition > 0), default=0)
        has_food = any(k in cfg.food and v > 0 for k, v in (a.inventory or {}).items())
        if a.hunger >= 50 and a.money_cents < cheapest and not has_food:
            add("broke_and_hungry", 3,
                f"{a.name} is hungry ({a.hunger:.0f}) with {a.money_cents / 100:.2f} asaps and no food; "
                f"job: {a.role}", a, {"hunger": a.hunger, "money_cents": a.money_cents})
        stuck = s.scalar(select(func.count(Event.id)).where(
            Event.agent_id == a.id, Event.kind.in_(("turned_away", "invalid_action")),
            Event.tick > ctx.tick - STUCK_WINDOW)) or 0
        if stuck >= STUCK_COUNT:
            last = s.scalar(select(Event.summary).where(
                Event.agent_id == a.id, Event.kind.in_(("turned_away", "invalid_action")))
                .order_by(Event.id.desc()).limit(1))
            add("stuck", 2, f"{a.name} failed {stuck} times in {STUCK_WINDOW}h; last: {last}", a,
                {"count": stuck})

    by_key = ctx.by_key
    if ctx.tick >= STALL_WINDOW:
        recent = s.scalars(select(Event).where(Event.kind == "work", Event.tick > ctx.tick - STALL_WINDOW))
        if not any((e.data or {}).get("success") for e in recent):
            bread = sum(economy.stock_qty(s, loc.id, "bread") for loc in by_key.values())
            add("production_stalled", 3,
                f"Nobody has done a productive hour of work in {STALL_WINDOW}h; bread on shelves: {bread}",
                None, {"bread": bread})
    if economy.shelf_food(s, cfg, by_key) == 0:
        served = ", ".join(cfg.economy.served) or "nothing"
        add("no_food_for_sale", 4,
            f"No food on any shop shelf; only the tavern serves ({served}) for money")
    for name, job in cfg.economy.jobs.items():
        if not job.makes or economy.workers_in(s, name):
            continue
        item = next(iter(job.makes))
        dest = by_key.get(job.deliver.get(item, job.workplace))
        qty = economy.stock_qty(s, dest.id, item) if dest else 0
        if qty < LOW_STOCK:
            add(f"no_workers_{name}", 2,
                f"Nobody works as {name}; {item} stock is {qty}"
                + (f" at {dest.name}" if dest else ""), None, {"job": name, "item": item, "qty": qty})
    s.add_all(found)
    s.flush()
    return found
