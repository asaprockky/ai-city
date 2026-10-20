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
from asapville.db.models import Agent, Location, WorldState
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


# --------------------------------------------------------------------------- economy
def _job_of(ctx: TickContext, agent: Agent):
    return ctx.settings.economy.jobs.get(agent.role)


APPLY_USAGE = 'apply {"job": "<job name>"}  -- take a job where you stand (the place lists its jobs)'


@register(ActionName.APPLY, APPLY_USAGE)
class Apply:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        job = str(d.action_args.get("job", "")).strip().lower()
        spec = ctx.settings.economy.jobs.get(job)
        if spec is None:
            return f"There is no job called '{job}'."
        here = _here(ctx, agent)
        if here is None or here.key != spec.workplace:
            return f"You apply for {job} at {ctx.by_key[spec.workplace].name}, not here."
        if agent.role == job:
            return f"You already work here as a {job}."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        job = str(d.action_args["job"]).strip().lower()
        spec = ctx.settings.economy.jobs[job]
        here = _here(ctx, agent)
        assert here is not None
        agent.role = job
        emit(ctx.session, tick=ctx.tick, kind="hired", actor=agent, location_id=here.id,
             summary=f"{agent.name} took a job as {job} at {here.name} ({spec.wage_cents / 100:.2f}/h)",
             data={"job": job, "wage_cents": spec.wage_cents},
             actor_memory=f"I took a job as {job} at {here.name}; it pays {spec.wage_cents / 100:.2f} "
                          f"asaps an hour. I work by choosing 'work' here.",
             witness_memory=f"{{actor}} signed on as a {job} here.", importance=4)
        return ActionResult(ok=True)


@register(ActionName.QUIT, "quit {}  -- leave your job")
class Quit:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        return None if _job_of(ctx, agent) else "You have no job to quit."

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        old = agent.role
        agent.role = "unemployed"
        emit(ctx.session, tick=ctx.tick, kind="quit", actor=agent, location_id=agent.location_id,
             summary=f"{agent.name} quit as {old}", data={"job": old},
             actor_memory=f"I quit my job as {old}.", importance=3)
        return ActionResult(ok=True)


@register(ActionName.WORK, "work {}  -- an hour at your job (you need one: apply at a workplace first)")
class Work:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        spec = _job_of(ctx, agent)
        if spec is None:
            return "You have no job. Apply at a workplace first (the farm, mill, bakery or quarry)."
        here = _here(ctx, agent)
        if here is None or here.key != spec.workplace:
            return f"Your job is at {ctx.by_key[spec.workplace].name}; you are not there."
        if agent.energy < ctx.settings.economy.work_energy_cost:
            return "You are too exhausted to work."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import economy

        spec = _job_of(ctx, agent)
        here = _here(ctx, agent)
        assert spec is not None and here is not None
        ws = ctx.session.get(WorldState, 1)
        assert ws is not None
        agent.energy = needs_rules.clamp(agent.energy - ctx.settings.economy.work_energy_cost)
        source = ctx.by_key.get(spec.source) if spec.source else None
        for item, qty in spec.uses.items():
            have = economy.stock_qty(ctx.session, source.id, item) if source else 0
            if have < qty:
                where = source.name if source else "anywhere"
                emit(ctx.session, tick=ctx.tick, kind="work", actor=agent, location_id=here.id,
                     summary=f"{agent.name} could not work as {agent.role}: no {item} at {where}",
                     data={"job": agent.role, "success": False, "missing": item},
                     actor_memory=f"I went to work as {agent.role} but there was no {item} from "
                                  f"{where}; nothing to do until someone makes some.",
                     witness_memory="{actor} stood idle at work; the supplies had run out.", importance=3)
                return ActionResult(ok=True)
        if ws.treasury_cents < spec.wage_cents:
            emit(ctx.session, tick=ctx.tick, kind="work", actor=agent, location_id=here.id,
                 summary=f"{agent.name} worked as {agent.role} but the town fund could not pay",
                 data={"job": agent.role, "success": False, "missing": "wages"},
                 actor_memory="The town could not pay my wages today; the fund is empty.", importance=4)
            return ActionResult(ok=True)
        for item, qty in spec.uses.items():
            economy.stock_add(ctx.session, source.id, item, -qty)  # type: ignore[union-attr]
        made = []
        for item, qty in spec.makes.items():
            dest = ctx.by_key.get(spec.deliver.get(item, spec.workplace), here)
            economy.stock_add(ctx.session, dest.id, item, qty)
            made.append(f"{qty} {item}" + (f" to {dest.name}" if dest.id != here.id else ""))
        ws.treasury_cents -= spec.wage_cents
        agent.money_cents += spec.wage_cents
        what = ", ".join(made) or "an hour's labour"
        emit(ctx.session, tick=ctx.tick, kind="work", actor=agent, location_id=here.id,
             summary=f"{agent.name} worked as {agent.role}: {what}, earned {spec.wage_cents / 100:.2f}",
             data={"job": agent.role, "success": True, "made": spec.makes, "wage_cents": spec.wage_cents},
             actor_memory=f"I worked an hour as {agent.role} ({what}) and earned "
                          f"{spec.wage_cents / 100:.2f} asaps.",
             witness_memory="{actor} was hard at work.", importance=2)
        return ActionResult(ok=True)


def _qty(d: Decision) -> int:
    try:
        return max(1, min(10, int(d.action_args.get("qty", 1))))
    except (TypeError, ValueError):
        return 1


@register(ActionName.BUY, 'buy {"item": "<item>", "qty": 1}  -- at a shop that sells it; prices are listed')
class Buy:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.world import economy

        here = _here(ctx, agent)
        item = str(d.action_args.get("item", "")).strip().lower()
        if here is None or item not in economy.shop_items(ctx.settings, here.key):
            sellers = [ctx.by_key[k].name for k, items in ctx.settings.economy.shops.items()
                       if item in items and k in ctx.by_key]
            hint = f" {sellers[0]} sells it." if sellers else ""
            return f"Nobody sells {item or 'that'} here.{hint}"
        qty = _qty(d)
        price = economy.price_of(ctx.settings, item) or 0
        if agent.money_cents < price * qty:
            return (f"{qty} {item} costs {price * qty / 100:.2f} asaps; "
                    f"you have {agent.money_cents / 100:.2f}.")
        left = economy.stock_qty(ctx.session, here.id, item)
        if item not in ctx.settings.economy.unlimited and left < qty:
            if left:
                return f"The shelf is nearly empty: only {left} {item} left."
            return f"There is no {item} left; someone has to make more."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import economy

        here = _here(ctx, agent)
        assert here is not None
        item = str(d.action_args["item"]).strip().lower()
        qty = _qty(d)
        price = economy.price_of(ctx.settings, item) or 0
        ws = ctx.session.get(WorldState, 1)
        assert ws is not None
        agent.money_cents -= price * qty
        ws.treasury_cents += price * qty
        if item not in ctx.settings.economy.unlimited:
            economy.stock_add(ctx.session, here.id, item, -qty)
        if item in ctx.settings.economy.served:
            agent.hunger = needs_rules.eat(agent.hunger, ctx.settings.food[item].nutrition * qty)
            what = f"ate {qty} {item} at {here.name}"
            mem = f"I paid {price * qty / 100:.2f} asaps for {item} at {here.name} and ate it there."
        else:
            inv = dict(agent.inventory or {})
            inv[item] = inv.get(item, 0) + qty
            agent.inventory = inv
            what = f"bought {qty} {item} at {here.name}"
            mem = f"I bought {qty} {item} at {here.name} for {price * qty / 100:.2f} asaps."
        emit(ctx.session, tick=ctx.tick, kind="buy", actor=agent, location_id=here.id,
             summary=f"{agent.name} {what} for {price * qty / 100:.2f}",
             data={"item": item, "qty": qty, "cents": price * qty}, actor_memory=mem,
             witness_memory=f"{{actor}} bought some {item}.", importance=2)
        return ActionResult(ok=True)


SELL_USAGE = 'sell {"item": "<item>", "qty": 1}  -- to a shop that trades it; it pays part of the shelf price'


@register(ActionName.SELL, SELL_USAGE)
class Sell:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.world import economy

        here = _here(ctx, agent)
        item = str(d.action_args.get("item", "")).strip().lower()
        if here is None or item not in economy.shop_items(ctx.settings, here.key) \
                or item in ctx.settings.economy.unlimited:
            return f"Nobody here buys {item or 'that'}."
        if (agent.inventory or {}).get(item, 0) < _qty(d):
            return f"You don't have {_qty(d)} {item} to sell."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import economy

        here = _here(ctx, agent)
        assert here is not None
        item = str(d.action_args["item"]).strip().lower()
        qty = _qty(d)
        cents = int((economy.price_of(ctx.settings, item) or 0) * ctx.settings.economy.buyback) * qty
        ws = ctx.session.get(WorldState, 1)
        assert ws is not None
        if ws.treasury_cents < cents:
            cents = ws.treasury_cents  # the town pays what it can
        inv = dict(agent.inventory or {})
        inv[item] -= qty
        if inv[item] <= 0:
            del inv[item]
        agent.inventory = inv
        agent.money_cents += cents
        ws.treasury_cents -= cents
        economy.stock_add(ctx.session, here.id, item, qty)
        emit(ctx.session, tick=ctx.tick, kind="sell", actor=agent, location_id=here.id,
             summary=f"{agent.name} sold {qty} {item} at {here.name} for {cents / 100:.2f}",
             data={"item": item, "qty": qty, "cents": cents},
             actor_memory=f"I sold {qty} {item} at {here.name} for {cents / 100:.2f} asaps.",
             witness_memory=f"{{actor}} sold some {item} to the shop.", importance=2)
        return ActionResult(ok=True)


NOTICE_USAGE = ('write_notice {"text": "<message for the whole town>"}  -- at the town hall or square; '
                'everyone reads it for a day')


@register(ActionName.WRITE_NOTICE, NOTICE_USAGE)
class WriteNotice:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        here = _here(ctx, agent)
        if here is None or here.kind not in ("town_hall", "square"):
            return "Notices go on the board at the Town Hall or the Town Square."
        if not str(d.action_args.get("text", "")).strip():
            return "Write something in 'text'."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        here = _here(ctx, agent)
        assert here is not None
        text = str(d.action_args["text"]).strip()[:300]
        emit(ctx.session, tick=ctx.tick, kind="notice", actor=agent, location_id=here.id,
             visibility="public", summary=f'{agent.name} pinned a notice: "{text}"',
             data={"text": text, "author_id": agent.id},
             actor_memory=f'I pinned a notice on the town board: "{text}"',
             witness_memory="{actor} pinned a notice on the board.", importance=3)
        return ActionResult(ok=True)
