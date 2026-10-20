"""The town's economy: shelves, the town fund, jobs and the production chain.

Money is conserved: citizens pay shops, shops pay the treasury, the treasury pays
wages. Goods are conserved too: nothing lands on a shelf unless a worker made it
(or an angel put it there, which the dashboard shows as an intervention).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.config import JobCfg, Settings
from asapville.db.models import Agent, Location, Stock, WorldState, WorldTick
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


def price_of(settings: Settings, item: str) -> int | None:
    return settings.economy.prices.get(item)


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
