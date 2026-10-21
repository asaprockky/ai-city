"""Per-tick agent snapshots: the observer's record of the town, tick by tick.

The engine writes one `AgentTick` row per agent at the end of every tick. The
dashboard reads them back to replay the town and streams the same payload live.
Citizens never see any of this.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from asapville.db.models import Agent, AgentTick


def record_tick(session: Session, tick: int, agents: list[Agent]) -> list[AgentTick]:
    rows = [
        AgentTick(
            tick=tick, agent_id=a.id, location_id=a.location_id, destination_id=a.destination_id,
            travel_ticks_left=a.travel_ticks_left, asleep=a.asleep, alive=a.alive,
            hunger=a.hunger, energy=a.energy, social=a.social, health=a.health, mood=a.mood,
            mood_label=a.mood_label, money_cents=a.money_cents, inventory=dict(a.inventory or {}),
            action=a.last_action, thought=a.last_thought,
        )
        for a in agents
    ]
    session.add_all(rows)
    return rows


def tick_payload(row: AgentTick) -> dict[str, Any]:
    """Compact JSON for one agent at one tick (same shape live and in replays)."""
    return {
        "id": row.agent_id, "loc": row.location_id, "dest": row.destination_id,
        "left": row.travel_ticks_left, "asleep": row.asleep, "alive": row.alive,
        "hunger": round(row.hunger), "energy": round(row.energy), "social": round(row.social),
        "health": round(row.health), "mood": round(row.mood), "mood_label": row.mood_label,
        "money": row.money_cents, "inventory": row.inventory or {},
        "action": row.action, "thought": row.thought,
    }
