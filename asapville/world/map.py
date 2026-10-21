"""Town map: named locations on a grid, travel time and capacity."""

from __future__ import annotations

import math

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from asapville.config import MapCfg
from asapville.db.models import Agent, Location


def home_plot_specs(cfg: MapCfg) -> list[dict]:
    hp = cfg.home_plots
    specs = []
    for i in range(hp.count):
        col, row = i % hp.columns, i // hp.columns
        specs.append({
            "key": f"home_{i + 1}",
            "name": f"House No. {i + 1}",
            "kind": "home",
            "x": hp.origin.x + col * hp.spacing,
            "y": hp.origin.y + row * hp.spacing,
            "capacity": hp.capacity,
        })
    return specs


def seed_locations(session: Session, cfg: MapCfg) -> list[Location]:
    """Create all configured locations (idempotent by key); places already built take the
    configured capacity, so a growing town can make room."""
    existing = {loc.key: loc for loc in session.scalars(select(Location))}
    created = []
    for spec in [loc.model_dump() for loc in cfg.locations] + home_plot_specs(cfg):
        if spec["key"] in existing:
            existing[spec["key"]].capacity = spec["capacity"]
            continue
        if not (0 <= spec["x"] < cfg.width and 0 <= spec["y"] < cfg.height):
            raise ValueError(f"location {spec['key']} is off the map")
        loc = Location(**spec)
        session.add(loc)
        created.append(loc)
    session.flush()
    return created


def distance(a: Location, b: Location) -> int:
    return abs(a.x - b.x) + abs(a.y - b.y)


def travel_ticks(a: Location, b: Location, cells_per_tick: int, slowdown: float = 1.0) -> int:
    """Ticks to walk from a to b. 0 if same place; at least 1 otherwise.

    `slowdown` > 1 models bad weather or injury (Phase 5 feeds it).
    """
    d = distance(a, b)
    if d == 0:
        return 0
    return max(1, math.ceil(d * slowdown / cells_per_tick))


def occupancy(session: Session, location_id: int) -> int:
    """Living agents physically at a location (not travellers heading there)."""
    return session.scalar(
        select(func.count(Agent.id)).where(
            Agent.location_id == location_id, Agent.alive.is_(True), Agent.travel_ticks_left == 0
        )
    ) or 0


def has_room(session: Session, location: Location) -> bool:
    return occupancy(session, location.id) < location.capacity
