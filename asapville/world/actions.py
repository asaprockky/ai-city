"""Action registry: validation (world rules) and execution (state mutation).

Only actions registered here are offered to agents. Anything else in the closed
`ActionName` enum is rejected as "not possible yet" until its phase lands.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from asapville.agents.schemas import ActionName, Decision
from asapville.config import Settings
from asapville.db.models import Agent, Location
from asapville.world import needs as needs_rules
from asapville.world.clock import WorldTime
from asapville.world.events import emit
from asapville.world.map import has_room, travel_ticks
from asapville.world.social import introduce


@dataclass
class TickContext:
    session: Session
    settings: Settings
    tick: int
    time: WorldTime
    rng: random.Random
    locations: dict[int, Location]
    weather: str = "clear"
    travel_slowdown: float = 1.0

    @property
    def by_key(self) -> dict[str, Location]:
        return {loc.key: loc for loc in self.locations.values()}


@dataclass
class ActionResult:
    ok: bool
    note: str = ""
    resting: bool = False
    socialised: set[int] = field(default_factory=set)
    fell_asleep: bool = False


Validator = Callable[[TickContext, Agent, Decision, set[int]], str | None]
Executor = Callable[[TickContext, Agent, Decision], ActionResult]


@dataclass(frozen=True)
class ActionSpec:
    name: ActionName
    usage: str  # shown to the agent, e.g. 'move {"to": "<place key>"}'
    validate: Validator
    execute: Executor


REGISTRY: dict[ActionName, ActionSpec] = {}


def register(name: ActionName, usage: str) -> Callable[[type], type]:
    def deco(cls: type) -> type:
        REGISTRY[name] = ActionSpec(name, usage, cls.validate, cls.execute)
        return cls
    return deco


def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
    spec = REGISTRY.get(d.action)
    if spec is None:
        return f"'{d.action.value}' isn't something you can do in Asapville yet."
    return spec.validate(ctx, agent, d, perceived)


def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
    return REGISTRY[d.action].execute(ctx, agent, d)


def _here(ctx: TickContext, agent: Agent) -> Location | None:
    return ctx.locations.get(agent.location_id) if agent.location_id else None


# --------------------------------------------------------------------------- move
def arrive(ctx: TickContext, agent: Agent) -> None:
    """Finish a journey. A full building turns the agent away to the square."""
    dest = ctx.locations[agent.destination_id] if agent.destination_id else None
    agent.destination_id = None
    agent.travel_ticks_left = 0
    if dest is None:
        return
    if not has_room(ctx.session, dest):
        fallback = ctx.by_key.get("square") or dest
        agent.location_id = fallback.id
        emit(ctx.session, tick=ctx.tick, kind="turned_away", actor=agent, location_id=fallback.id,
             summary=f"{agent.name} found {dest.name} full and ended up at {fallback.name}",
             actor_memory=f"{dest.name} was full; I couldn't get in, so I went to {fallback.name}.",
             importance=3)
        return
    agent.location_id = dest.id
    emit(ctx.session, tick=ctx.tick, kind="arrive", actor=agent, location_id=dest.id,
         summary=f"{agent.name} arrived at {dest.name}",
         actor_memory=f"I arrived at {dest.name}.", witness_memory="{actor} arrived.",
         importance=1)


@register(ActionName.MOVE, 'move {"to": "<place key>"}  -- walk somewhere (takes time)')
class Move:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        key = d.action_args.get("to")
        dest = ctx.by_key.get(str(key)) if key is not None else None
        if dest is None:
            return f"You don't know any place called '{key}'."
        if dest.id == agent.location_id:
            return f"You are already at {dest.name}."
        if dest.kind == "home" and dest.id != agent.home_id:
            return f"{dest.name} is someone else's home; you have no reason to walk in."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        dest = ctx.by_key[str(d.action_args["to"])]
        origin = _here(ctx, agent)
        hours = travel_ticks(origin, dest, ctx.settings.map.cells_per_tick,
                             ctx.travel_slowdown) if origin else 1
        if origin:
            emit(ctx.session, tick=ctx.tick, kind="depart", actor=agent, location_id=origin.id,
                 summary=f"{agent.name} left {origin.name} for {dest.name} ({hours}h)",
                 data={"from": origin.id, "to": dest.id, "hours": hours},
                 actor_memory=f"I set off from {origin.name} toward {dest.name} "
                              f"(about {hours} hour{'s' if hours != 1 else ''}).",
                 importance=1)
        agent.location_id = None
        agent.destination_id = dest.id
        agent.travel_ticks_left = hours - 1  # this hour is the first hour of walking
        if agent.travel_ticks_left <= 0:
            arrive(ctx, agent)
        return ActionResult(ok=True)


# --------------------------------------------------------------------------- body
@register(ActionName.REST, "rest {}  -- sit down and recover a little energy")
class Rest:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        return ActionResult(ok=True, resting=True)


@register(ActionName.SLEEP, "sleep {}  -- lie down and sleep now (best at home)")
class Sleep:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        where = "at home" if agent.location_id == agent.home_id else "out here, without a bed"
        emit(ctx.session, tick=ctx.tick, kind="sleep", actor=agent, location_id=agent.location_id,
             summary=f"{agent.name} went to sleep", actor_memory=f"I lay down to sleep {where}.",
             witness_memory="{actor} lay down and fell asleep.", importance=1)
        return ActionResult(ok=True, fell_asleep=True)


@register(ActionName.EAT, 'eat {"item": "<food in your inventory>"}')
class Eat:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        item = str(d.action_args.get("item", ""))
        if item not in ctx.settings.food:
            return f"'{item}' isn't food."
        if (agent.inventory or {}).get(item, 0) <= 0:
            return f"You don't have any {item}."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        item = str(d.action_args["item"])
        inv = dict(agent.inventory or {})
        inv[item] -= 1
        if inv[item] <= 0:
            del inv[item]
        agent.inventory = inv  # reassign so the JSON column is flagged dirty
        agent.hunger = needs_rules.eat(agent.hunger, ctx.settings.food[item].nutrition)
        emit(ctx.session, tick=ctx.tick, kind="eat", actor=agent, location_id=agent.location_id,
             summary=f"{agent.name} ate {item}", actor_memory=f"I ate some {item}.",
             witness_memory=f"{{actor}} ate some {item}.", importance=1)
        return ActionResult(ok=True)


@register(ActionName.FORAGE, "forage {}  -- search the wild for food (forest, river); tiring, not guaranteed")
class Forage:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        here = _here(ctx, agent)
        if here is None or here.kind not in ctx.settings.forage:
            return "There is nothing to forage here."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        here = _here(ctx, agent)
        assert here is not None
        spot = ctx.settings.forage[here.kind]
        agent.energy = needs_rules.clamp(agent.energy - spot.energy_cost)
        if ctx.rng.random() < spot.chance:
            qty = ctx.rng.randint(1, spot.max_qty)
            inv = dict(agent.inventory or {})
            inv[spot.item] = inv.get(spot.item, 0) + qty
            agent.inventory = inv
            emit(ctx.session, tick=ctx.tick, kind="forage", actor=agent, location_id=here.id,
                 summary=f"{agent.name} foraged {qty} {spot.item} at {here.name}",
                 data={"item": spot.item, "qty": qty, "success": True},
                 actor_memory=f"I foraged at {here.name} and found {qty} {spot.item}.",
                 witness_memory=f"{{actor}} gathered some {spot.item}.", importance=2)
        else:
            emit(ctx.session, tick=ctx.tick, kind="forage", actor=agent, location_id=here.id,
                 summary=f"{agent.name} foraged at {here.name} and found nothing",
                 data={"item": spot.item, "qty": 0, "success": False},
                 actor_memory=f"I searched {here.name} for food for an hour and found nothing.",
                 importance=2)
        return ActionResult(ok=True)


# --------------------------------------------------------------------------- social
@register(ActionName.TALK, 'talk {"to": <ref of someone here>}  -- put your words in "dialogue"')
class Talk:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        ref = d.action_args.get("to")
        try:
            target_id = int(ref)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "Talk to whom? Give the ref of someone who is here."
        if target_id not in perceived or target_id == agent.id:
            return "There's nobody like that here to talk to."
        target = ctx.session.get(Agent, target_id)
        if target is None or not target.alive or target.location_id != agent.location_id \
                or target.travel_ticks_left > 0:
            return "They're not here any more."
        if target.asleep:
            return "They're asleep."
        if not (d.dialogue or "").strip():
            return "You opened your mouth but said nothing; put your words in 'dialogue'."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        target = ctx.session.get(Agent, int(d.action_args["to"]))
        assert target is not None
        line = (d.dialogue or "").strip()
        # Memories are written before introductions, so the first meeting reads as a stranger.
        emit(ctx.session, tick=ctx.tick, kind="talk", actor=agent, target=target,
             location_id=agent.location_id, summary=f'{agent.name} to {target.name}: "{line}"',
             data={"line": line}, memory_kind="dialogue",
             actor_memory=f'I said to {{target}}: "{line}"',
             target_memory=f'{{actor}} said to me: "{line}"',
             witness_memory=f'{{actor}} said to {{target}}: "{line}"', importance=2)
        introduce(ctx.session, agent, target, ctx.tick)
        return ActionResult(ok=True, socialised={agent.id, target.id})


@register(ActionName.IDLE, "idle {}  -- do nothing in particular this hour")
class Idle:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        return ActionResult(ok=True)


def available_usages() -> list[str]:
    return [spec.usage for spec in REGISTRY.values()]
