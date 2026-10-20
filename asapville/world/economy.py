"""The town's economy: shelves, the town fund, jobs and the production chain.

Money is conserved: citizens pay shops, shops pay the treasury, the treasury pays
wages. Goods are conserved too: nothing lands on a shelf unless a worker made it
(or an angel put it there, which the dashboard shows as an intervention).
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.config import JobCfg, Settings
from asapville.db.models import Agent, Brand, Location, Stall, Stock, WorldState, WorldTick
from asapville.world.events import emit


def stock_qty(session: Session, location_id: int, item: str) -> int:
    row = session.scalar(select(Stock).where(Stock.location_id == location_id, Stock.item == item))
    return row.qty if row else 0


def stock_add(session: Session, location_id: int, item: str, delta: int) -> int:
    """Change a shelf by `delta` (never below zero). Returns the new quantity."""
    row = session.scalar(select(Stock).where(Stock.location_id == location_id, Stock.item == item))
    if row is None:
        row = Stock(location_id=location_id, item=item, qty=0)
        session.add(row)
    if row.qty + delta < 0:
        raise ValueError(f"only {row.qty} {item} here")
    row.qty += delta
    session.flush()
    return row.qty


def stocks_by_location(session: Session, locations: dict[int, Location]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in session.scalars(select(Stock).where(Stock.qty > 0).order_by(Stock.location_id, Stock.item)):
        loc = locations.get(row.location_id)
        if loc is not None:
            out.setdefault(loc.key, {})[row.item] = row.qty
    return out


def price_of(settings: Settings, item: str, session: Session | None = None) -> int | None:
    """Shelf price in cents: the town's vote if there was one, else config."""
    if session is not None:
        ws = session.get(WorldState, 1)
        if ws is not None and item in (ws.prices or {}):
            return int(ws.prices[item])
    return settings.economy.prices.get(item)


def prices_now(session: Session, settings: Settings) -> dict[str, int]:
    return {item: price_of(settings, item, session) or 0 for item in settings.economy.prices}


def shop_items(settings: Settings, location_key: str) -> list[str]:
    return settings.economy.shops.get(location_key, [])


def jobs_at(settings: Settings, location_key: str) -> dict[str, JobCfg]:
    return {name: job for name, job in settings.economy.jobs.items() if job.workplace == location_key}


def workers_in(session: Session, job: str) -> list[Agent]:
    return list(session.scalars(select(Agent).where(Agent.role == job, Agent.alive.is_(True))
                                .order_by(Agent.id)))


def shelf_food(session: Session, settings: Settings, by_key: dict[str, Location]) -> int:
    """Units of food on shop shelves that can run out (the tavern kitchen doesn't count)."""
    total = 0
    for key, items in settings.economy.shops.items():
        loc = by_key.get(key)
        if loc is None:
            continue
        for item in items:
            if item in settings.food and item not in settings.economy.unlimited:
                total += stock_qty(session, loc.id, item)
    return total


def open_economy(session: Session, settings: Settings, *, tick: int) -> bool:
    """Seed the shelves and the town fund once. Idempotent; returns True the first time."""
    ws = session.get(WorldState, 1)
    if ws is None:
        return False
    if session.scalar(select(Stock.id).limit(1)) is not None:
        return False
    by_key = {loc.key: loc for loc in session.scalars(select(Location))}
    for key, items in settings.economy.stock_start.items():
        loc = by_key.get(key)
        if loc is None:
            continue
        for item, qty in items.items():
            stock_add(session, loc.id, item, qty)
    if ws.treasury_cents == 0:
        ws.treasury_cents = settings.economy.treasury_cents
    shops = ", ".join(by_key[k].name for k in settings.economy.shops if k in by_key)
    jobs = ", ".join(f"{name} at {by_key[j.workplace].name}" for name, j in settings.economy.jobs.items()
                     if j.workplace in by_key)
    emit(session, tick=tick, kind="notice", visibility="public",
         summary=f"Town notice: the shops are open ({shops}); the town hires {jobs}",
         data={"text": f"The shops of Asapville are open: {shops}. Bread is baked from flour, flour "
                       f"is milled from grain, grain is grown on the farm; nothing appears on a shelf "
                       f"unless someone makes it. The town pays wages for honest work: {jobs}. "
                       f"Apply at the workplace.", "author": "the town council"})
    session.flush()
    return True


def record_world_tick(session: Session, tick: int, ws: WorldState,
                      locations: dict[int, Location]) -> WorldTick:
    row = WorldTick(tick=tick, treasury_cents=ws.treasury_cents,
                    stocks=stocks_by_location(session, locations), weather=ws.weather)
    session.add(row)
    return row


def world_tick_payload(row: WorldTick) -> dict[str, Any]:
    return {"treasury": row.treasury_cents, "stocks": row.stocks or {}}


MAX_STALLS = 3
_ITEM = re.compile(r"[^a-z' -]+")


def clean_item(name: object) -> str:
    return re.sub(r"\s+", " ", _ITEM.sub("", str(name).lower())).strip()[:32]


def stalls_at(session: Session, location_id: int) -> list[Stall]:
    return list(session.scalars(select(Stall).where(Stall.location_id == location_id).order_by(Stall.id)))


def own_stall(session: Session, agent: Agent, here: Location | None, item: str) -> Stall | None:
    if here is None:
        return None
    return session.scalar(select(Stall).where(Stall.owner_id == agent.id, Stall.location_id == here.id,
                                              Stall.item == item))


def stall_for(session: Session, here: Location | None, item: str, name: object, *,
              buyer: Agent) -> Stall | str | None:
    """The citizen stall a purchase goes to: the one named in `from`, else the cheapest with stock.

    None means "no stall here sells it" (the town shop may); a string explains a failed `from`.
    """
    if here is None:
        return None
    options = [s for s in stalls_at(session, here.id) if s.item == item and s.owner_id != buyer.id]
    if name:
        wanted = str(name).strip().lower()
        named = [s for s in options if s.name.lower() == wanted]
        if not named:
            return f"There's no stall called '{name}' selling {item} here."
        return named[0]
    stocked = [s for s in options if s.qty > 0]
    return min(stocked, key=lambda s: s.price_cents) if stocked else None


def can_open_stall(session: Session, agent: Agent, here: Location, name: str, item: str) -> str | None:
    if own_stall(session, agent, here, item) is not None:
        return f"You already sell {item} here."
    if len(list(session.scalars(select(Stall).where(Stall.owner_id == agent.id)))) >= MAX_STALLS:
        return f"You already run {MAX_STALLS} businesses."
    taken = session.scalar(select(Brand).where(Brand.name == name[:60]))
    if taken is not None and taken.owner_id != agent.id:
        return f"'{name}' is already somebody's business name."
    return None


def open_stall(session: Session, agent: Agent, here: Location, name: str, item: str,
               price_cents: int, tick: int) -> Stall:
    brand = session.scalar(select(Brand).where(Brand.name == name))
    if brand is None:
        brand = Brand(name=name, category="stall", owner_id=agent.id, founded_tick=tick, reputation=0.0)
        session.add(brand)
        session.flush()
    stall = Stall(owner_id=agent.id, brand_id=brand.id, location_id=here.id, name=name, item=item,
                  price_cents=price_cents, qty=0, sold=0, opened_tick=tick)
    session.add(stall)
    session.flush()
    return stall


def stall_payload(s: Stall) -> dict[str, Any]:
    return {"id": s.id, "owner_id": s.owner_id, "location_id": s.location_id, "name": s.name,
            "item": s.item, "price": s.price_cents, "qty": s.qty, "sold": s.sold, "opened": s.opened_tick}


# The labour market: each midnight a job nobody holds pays more, a crowded one pays less.
WAGE_UP, WAGE_DOWN, CROWDED = 1.25, 0.9, 3
SHORT_HOURS = 3  # idle hours downstream, in a day, that count as a shortage of this job's output
WAGE_MIN, WAGE_MAX = 0.5, 3.0  # bounds, as multiples of the base wage in config


def wage_of(session: Session, settings: Settings, job: str) -> int:
    ws = session.get(WorldState, 1)
    base = settings.economy.jobs[job].wage_cents
    return int((ws.wages or {}).get(job, base)) if ws is not None else base


def shortages(session: Session, settings: Settings, since_tick: int) -> dict[str, int]:
    """Per job, how many hours others could not work for want of what this job makes.

    The blame walks up the chain: bakers idle for flour count against the miller only if the
    millers themselves were working; if the millers stood idle for grain, it is the farmer's
    shortage (more millers would not bake a single loaf).
    """
    from asapville.db.models import Event

    makers = {item: name for name, job in settings.economy.jobs.items() for item in job.makes}
    worked: dict[str, int] = {}
    blocked_on: dict[str, dict[str, int]] = {}  # job -> missing item -> hours
    for e in session.scalars(select(Event).where(Event.kind == "work", Event.tick >= since_tick)):
        d = e.data or {}
        job = d.get("job")
        if d.get("success"):
            worked[job] = worked.get(job, 0) + 1
        elif d.get("missing") in makers:
            blocked_on.setdefault(job, {})
            blocked_on[job][d["missing"]] = blocked_on[job].get(d["missing"], 0) + 1
    out: dict[str, int] = {}
    for job, missing in blocked_on.items():
        for item, hours in missing.items():
            maker, seen = makers[item], {job}
            while maker not in seen:  # pass the blame up while the maker was mostly idle too
                seen.add(maker)
                up = blocked_on.get(maker, {})
                if not up or sum(up.values()) <= worked.get(maker, 0):
                    break
                maker = makers[max(up, key=lambda k: up[k])]
            out[maker] = out.get(maker, 0) + hours
    return out


def adjust_wages(session: Session, settings: Settings, *,
                 tick: int | None = None) -> dict[str, tuple[int, int]]:
    """Move every wage one step toward what the town needs. Returns {job: (old, new)} for changes.

    A job pays more when nobody holds it, or when others stood idle waiting for what it makes
    (a miller who doesn't mill raises the miller's wage); it pays less when crowded and not short.
    """
    ws = session.get(WorldState, 1)
    assert ws is not None
    wages = dict(ws.wages or {})
    short = shortages(session, settings, (tick if tick is not None else ws.tick) - 24)
    changed = {}
    for name, job in settings.economy.jobs.items():
        old = int(wages.get(name, job.wage_cents))
        n = len(workers_in(session, name))
        needed = n == 0 or short.get(name, 0) >= SHORT_HOURS
        factor = WAGE_UP if needed else WAGE_DOWN if n > CROWDED else 1.0
        new = round(min(job.wage_cents * WAGE_MAX, max(job.wage_cents * WAGE_MIN, old * factor)))
        wages[name] = new
        if new != old:
            changed[name] = (old, new)
    ws.wages = wages
    return changed
