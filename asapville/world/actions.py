"""Action registry: validation (world rules) and execution (state mutation).

Only actions registered here are offered to agents. Anything else in the closed
`ActionName` enum is rejected as "not possible yet" until its phase lands.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.agents.schemas import ActionName, Decision
from asapville.config import Settings
from asapville.db.models import Agent, Location, WorldState
from asapville.world import needs as needs_rules
from asapville.world.clock import WorldTime
from asapville.world.events import emit, remember
from asapville.world.map import has_room, travel_ticks
from asapville.world.social import introduce, nudge_reputation, record_deed


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
    improvised: list[dict[str, Any]] = field(default_factory=list)  # judged after everyone acts

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


def _savour(ctx: TickContext, agent: Agent, item: str) -> str | None:
    """Taste: the same food three times running spoils the mood; something new lifts it."""
    diet, change, feeling = needs_rules.taste(list(agent.diet or []), item, ctx.settings.needs)
    agent.diet = diet
    treat = ctx.settings.food[item].mood if item in ctx.settings.food else 0.0
    agent.mood = needs_rules.clamp(agent.mood + change + treat)
    if treat >= 5:
        feeling = "delicious" + (f"; {feeling}" if feeling else "")
    return feeling


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
        food = ctx.settings.food[item]
        agent.hunger = needs_rules.eat(agent.hunger, food.nutrition)
        agent.health = needs_rules.clamp(agent.health + food.health)
        feeling = _savour(ctx, agent, item)
        emit(ctx.session, tick=ctx.tick, kind="eat", actor=agent, location_id=agent.location_id,
             summary=f"{agent.name} ate {item}" + (f" ({feeling})" if feeling else ""),
             data={"item": item, "feeling": feeling},
             actor_memory=f"I ate some {item}." + (f" {feeling.capitalize()}." if feeling else ""),
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
            return f"You already work here as a {job}. Choose 'work' to do a shift."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        job = str(d.action_args["job"]).strip().lower()
        here = _here(ctx, agent)
        assert here is not None
        agent.role = job
        from asapville.world import economy

        wage = economy.wage_of(ctx.session, ctx.settings, job)
        emit(ctx.session, tick=ctx.tick, kind="hired", actor=agent, location_id=here.id,
             summary=f"{agent.name} took a job as {job} at {here.name} ({wage / 100:.2f}/h)",
             data={"job": job, "wage_cents": wage},
             actor_memory=f"I took a job as {job} at {here.name}; it pays {wage / 100:.2f} "
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
        cap = ctx.settings.economy.shelf_cap
        full = [item for item in spec.makes
                if economy.stock_qty(ctx.session, ctx.by_key.get(spec.deliver.get(item, spec.workplace),
                                                                 here).id, item) >= cap]
        if spec.makes and len(full) == len(spec.makes):
            emit(ctx.session, tick=ctx.tick, kind="work", actor=agent, location_id=here.id,
                 summary=f"{agent.name} came to work as {agent.role}, but nobody is buying: "
                         f"the shelves are full of {', '.join(full)}",
                 data={"job": agent.role, "success": False, "missing": "demand", "full": full},
                 actor_memory=f"No work and no pay today: the shelves are already full of {', '.join(full)} "
                              "that nobody is buying. Until people buy, there is nothing to make.",
                 importance=4)
            return ActionResult(ok=True)
        wage = economy.wage_of(ctx.session, ctx.settings, agent.role)
        boss = economy.owner_of(ctx.session, here)
        if boss is not None and boss.id == agent.id:
            wage = 0  # an owner working their own business pays nobody
        material = 0  # raw material bought from another business's shelf
        if source is not None and not economy.same_purse(ctx.session, here, source):
            material = sum((economy.price_of(ctx.settings, item, ctx.session, at=source.key) or 0) * qty
                           for item, qty in spec.uses.items())
        if economy.funds(ctx.session, here) < wage + material:
            payer = f"{boss.name} could not pay" if boss else "the town fund could not pay"
            emit(ctx.session, tick=ctx.tick, kind="work", actor=agent, location_id=here.id,
                 summary=f"{agent.name} came to work as {agent.role} but {payer}",
                 data={"job": agent.role, "success": False, "missing": "wages"},
                 actor_memory=(f"I came to work, but {boss.name}, who owns {here.name}, could not pay "
                               "my wage and the materials." if boss else
                               "The town could not pay my wages today; the fund is empty."),
                 importance=4)
            return ActionResult(ok=True)
        for item, qty in spec.uses.items():
            economy.stock_add(ctx.session, source.id, item, -qty)  # type: ignore[union-attr]
        economy.pay(ctx.session, here, source, material)
        made = []
        for item, qty in spec.makes.items():
            dest = ctx.by_key.get(spec.deliver.get(item, spec.workplace), here)
            kept = min(qty, spec.keep.get(item, 0)) if dest.id != here.id else 0
            if kept:  # a farm-gate share stays where it was made
                economy.stock_add(ctx.session, here.id, item, kept)
            if qty - kept:
                economy.stock_add(ctx.session, dest.id, item, qty - kept)
                # the receiving shop buys what is delivered to it, at the maker's price
                economy.pay(ctx.session, dest, here,
                            (economy.price_of(ctx.settings, item, ctx.session, at=here.key) or 0)
                            * (qty - kept))
            made.append(f"{qty} {item}" + (f" ({qty - kept} to {dest.name})" if kept else
                                          f" to {dest.name}" if dest.id != here.id else ""))
        economy.credit(ctx.session, here, -wage)
        taxed = economy.tax(ctx.session, wage, ctx.settings.economy.income_tax)
        agent.money_cents += wage - taxed
        what = ", ".join(made) or "an hour's labour"
        emit(ctx.session, tick=ctx.tick, kind="work", actor=agent, location_id=here.id,
             summary=f"{agent.name} worked as {agent.role}: {what}, earned {wage / 100:.2f}",
             data={"job": agent.role, "success": True, "made": spec.makes, "wage_cents": wage,
                   "tax_cents": taxed},
             actor_memory=f"I worked an hour as {agent.role} ({what}) and earned "
                          f"{wage / 100:.2f} asaps ({(wage - taxed) / 100:.2f} after the town's tax).",
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
        stall = economy.stall_for(ctx.session, here, item, d.action_args.get("from"), buyer=agent) \
            if here is not None else None
        if isinstance(stall, str):
            return stall
        if stall is not None:
            qty = _qty(d)
            if stall.qty < qty:
                return f"{stall.name} has only {stall.qty} {item} left."
            if agent.money_cents < stall.price_cents * qty:
                return (f"{qty} {item} at {stall.name} costs {stall.price_cents * qty / 100:.2f} asaps; "
                        f"you have {agent.money_cents / 100:.2f}.")
            return None
        if here is None or item not in economy.shop_items(ctx.settings, here.key):
            sellers = [ctx.by_key[k].name for k, items in ctx.settings.economy.shops.items()
                       if item in items and k in ctx.by_key]
            hint = f" {sellers[0]} sells it." if sellers else ""
            return f"Nobody sells {item or 'that'} here.{hint}"
        qty = _qty(d)
        price = economy.price_of(ctx.settings, item, ctx.session, at=here.key) or 0
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
        stall = economy.stall_for(ctx.session, here, item, d.action_args.get("from"), buyer=agent)
        if stall is not None and not isinstance(stall, str):
            return _buy_from_stall(ctx, agent, stall, qty)
        price = economy.price_of(ctx.settings, item, ctx.session, at=here.key) or 0
        agent.money_cents -= price * qty
        taxed = economy.tax(ctx.session, price * qty, ctx.settings.economy.sales_tax) \
            if economy.owner_of(ctx.session, here) is not None else 0
        economy.credit(ctx.session, here, price * qty - taxed)  # the shop's owner, or the town fund
        if item not in ctx.settings.economy.unlimited:
            economy.stock_add(ctx.session, here.id, item, -qty)
        if item in ctx.settings.economy.served:
            food = ctx.settings.food[item]
            agent.hunger = needs_rules.eat(agent.hunger, food.nutrition * qty)
            agent.health = needs_rules.clamp(agent.health + food.health * qty)
            feeling = _savour(ctx, agent, item) if food.nutrition > 0 else None
            what = f"ate {qty} {item} at {here.name}" + (f" ({feeling})" if feeling else "")
            mem = f"I paid {price * qty / 100:.2f} asaps for {item} at {here.name} and ate it there." \
                + (f" {feeling.capitalize()}." if feeling else "")
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


def _buy_from_stall(ctx: TickContext, agent: Agent, stall, qty: int) -> ActionResult:
    from asapville.world.events import remember

    owner = ctx.session.get(Agent, stall.owner_id)
    from asapville.world import economy

    cents = stall.price_cents * qty
    agent.money_cents -= cents
    if owner is not None:
        owner.money_cents += cents - economy.tax(ctx.session, cents, ctx.settings.economy.sales_tax)
    stall.qty -= qty
    stall.sold += qty
    inv = dict(agent.inventory or {})
    inv[stall.item] = inv.get(stall.item, 0) + qty
    agent.inventory = inv
    ev = emit(ctx.session, tick=ctx.tick, kind="buy", actor=agent, target=owner,
              location_id=stall.location_id,
              summary=f"{agent.name} bought {qty} {stall.item} from {stall.name} for {cents / 100:.2f}",
              data={"item": stall.item, "qty": qty, "cents": cents, "stall": stall.name,
                    "stall_id": stall.id, "owner_id": stall.owner_id},
              actor_memory=f"I bought {qty} {stall.item} from {stall.name} for {cents / 100:.2f} asaps.",
              witness_memory=f"{{actor}} bought {stall.item} from {stall.name}.", importance=2)
    if owner is not None and owner.id != agent.id:
        from asapville.world.social import describe

        record_deed(ctx.session, owner.id, agent, ctx.tick, f"bought {stall.item} from my stall", trust=0.05)

        remember(ctx.session, owner.id, ctx.tick, "outcome",
                 f"{stall.name} sold {qty} {stall.item} to {describe(ctx.session, owner.id, agent)} "
                 f"for {cents / 100:.2f} asaps ({stall.qty} left).", importance=3, event_id=ev.id)
    return ActionResult(ok=True)


OPEN_USAGE = ('open_stall {"name": "<your business name>", "item": "<what you sell>", "price": 2.5}'
              '  -- start your own business right here; you set the price, the money is yours')


@register(ActionName.OPEN_STALL, OPEN_USAGE)
class OpenStall:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.world import economy

        here = _here(ctx, agent)
        if here is None:
            return "You need to stand somewhere to open a business."
        if here.kind == "home" and here.id != agent.home_id:
            return "You can't open a business in someone else's home."
        if here.kind == "shop" and here.owner_id != agent.id:
            if here.owner_id is None:
                price = ctx.settings.economy.property_price_cents / 100
                return f"{here.name} is for sale ({price:.2f} asaps); buy it with buy_property first."
            owner = ctx.session.get(Agent, here.owner_id)
            return f"{here.name} belongs to {owner.name if owner else 'someone'}; only they trade here."
        name = str(d.action_args.get("name", "")).strip()
        item = economy.clean_item(d.action_args.get("item", ""))
        if len(name) < 2 or not item:
            return "Name your business and what it sells."
        try:
            price = round(float(d.action_args.get("price", 0)) * 100)
        except (TypeError, ValueError):
            return "Give a price in asaps, like 2.5."
        if not 10 <= price <= 100_000:
            return "Set a price between 0.10 and 1000 asaps."
        return economy.can_open_stall(ctx.session, agent, here, name, item)

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import economy

        here = _here(ctx, agent)
        assert here is not None
        stall = economy.open_stall(ctx.session, agent, here, str(d.action_args["name"]).strip()[:60],
                                   economy.clean_item(d.action_args["item"]),
                                   round(float(d.action_args["price"]) * 100), ctx.tick)
        emit(ctx.session, tick=ctx.tick, kind="open_stall", actor=agent, location_id=here.id,
             visibility="public", summary=f"{agent.name} opened {stall.name} at {here.name}, "
                                          f"selling {stall.item} at {stall.price_cents / 100:.2f}",
             data={"stall": stall.name, "stall_id": stall.id, "item": stall.item, "price": stall.price_cents},
             actor_memory=f"I opened my own business, {stall.name}, at {here.name}: {stall.item} at "
                          f"{stall.price_cents / 100:.2f} asaps. I stock it with 'stock_stall' here.",
             witness_memory=f"{{actor}} set up a business here: {stall.name}, selling {stall.item}.",
             importance=6)
        return ActionResult(ok=True)


STOCK_USAGE = ('stock_stall {"item": "<item>", "qty": 3, "price": 2.5}  -- put goods from your bag '
               'on your own stall here (price optional: change it any time)')


@register(ActionName.STOCK_STALL, STOCK_USAGE)
class StockStall:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.world import economy

        here = _here(ctx, agent)
        item = economy.clean_item(d.action_args.get("item", ""))
        stall = economy.own_stall(ctx.session, agent, here, item) if here is not None else None
        if stall is None:
            return f"You have no stall selling {item or 'that'} here. Open one with open_stall."
        qty = int(d.action_args.get("qty", 0) or 0)
        if qty < 0 or (agent.inventory or {}).get(item, 0) < qty:
            return f"You don't have {qty} {item}."
        if qty == 0 and "price" not in d.action_args:
            return "Stock something (qty) or change the price."
        if "price" in d.action_args:
            try:
                price = round(float(d.action_args["price"]) * 100)
            except (TypeError, ValueError):
                return "Give a price in asaps, like 2.5."
            if not 10 <= price <= 100_000:
                return "Set a price between 0.10 and 1000 asaps."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import economy

        here = _here(ctx, agent)
        item = economy.clean_item(d.action_args["item"])
        stall = economy.own_stall(ctx.session, agent, here, item)
        assert stall is not None and here is not None
        qty = int(d.action_args.get("qty", 0) or 0)
        if qty:
            inv = dict(agent.inventory or {})
            inv[item] -= qty
            if inv[item] <= 0:
                del inv[item]
            agent.inventory = inv
            stall.qty += qty
        if "price" in d.action_args:
            stall.price_cents = round(float(d.action_args["price"]) * 100)
        offer = f"{stall.qty} {item} at {stall.price_cents / 100:.2f}"
        emit(ctx.session, tick=ctx.tick, kind="stock_stall", actor=agent, location_id=here.id,
             summary=f"{agent.name} stocked {stall.name}: {offer}",
             data={"stall": stall.name, "stall_id": stall.id, "item": item, "added": qty,
                   "qty": stall.qty, "price": stall.price_cents},
             actor_memory=f"I stocked {stall.name}: {offer} asaps.",
             witness_memory=f"{{actor}} laid out {item} at {stall.name}.", importance=2)
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
        shelf = economy.price_of(ctx.settings, item, ctx.session, at=here.key) or 0
        cents = min(int(shelf * ctx.settings.economy.buyback) * qty, economy.funds(ctx.session, here))
        inv = dict(agent.inventory or {})
        inv[item] -= qty
        if inv[item] <= 0:
            del inv[item]
        agent.inventory = inv
        agent.money_cents += cents
        economy.credit(ctx.session, here, -cents)  # the shop pays what it can
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


# --------------------------------------------------------------------------- freedom
def _person_here(ctx: TickContext, agent: Agent, ref: Any, perceived: set[int]) -> Agent | str:
    """The awake person `ref` standing here, or why not."""
    try:
        target_id = int(ref)
    except (TypeError, ValueError):
        return "Give the ref of someone who is here."
    if target_id not in perceived or target_id == agent.id:
        return "There's nobody like that here."
    target = ctx.session.get(Agent, target_id)
    if target is None or not target.alive or target.location_id != agent.location_id \
            or target.travel_ticks_left > 0:
        return "They're not here any more."
    if target.asleep:
        return "They're asleep."
    return target


GIVE_USAGE = ('give {"to": <ref of someone here>, "item": "<item>", "qty": 1}  or  '
              'give {"to": <ref>, "asaps": 2.5}  -- hand over goods or money')


@register(ActionName.GIVE, GIVE_USAGE)
class Give:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        target = _person_here(ctx, agent, d.action_args.get("to"), perceived)
        if isinstance(target, str):
            return target
        if "asaps" in d.action_args:
            try:
                cents = round(float(d.action_args["asaps"]) * 100)
            except (TypeError, ValueError):
                return "How many asaps? Give a number."
            if cents <= 0:
                return "Give a positive amount."
            if cents > agent.money_cents:
                return f"You only have {agent.money_cents / 100:.2f} asaps."
            return None
        item = str(d.action_args.get("item", "")).strip().lower()
        if not item:
            return "Give what? Name an item or an amount of asaps."
        if (agent.inventory or {}).get(item, 0) < _qty(d):
            return f"You don't have {_qty(d)} {item}."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        target = ctx.session.get(Agent, int(d.action_args["to"]))
        assert target is not None
        if "asaps" in d.action_args:
            cents = round(float(d.action_args["asaps"]) * 100)
            agent.money_cents -= cents
            target.money_cents += cents
            what, data = f"{cents / 100:.2f} asaps", {"cents": cents}
        else:
            item, qty = str(d.action_args["item"]).strip().lower(), _qty(d)
            mine, theirs = dict(agent.inventory or {}), dict(target.inventory or {})
            mine[item] -= qty
            if mine[item] <= 0:
                del mine[item]
            theirs[item] = theirs.get(item, 0) + qty
            agent.inventory, target.inventory = mine, theirs
            what, data = f"{qty} {item}", {"item": item, "qty": qty}
        emit(ctx.session, tick=ctx.tick, kind="give", actor=agent, target=target,
             location_id=agent.location_id, summary=f"{agent.name} gave {target.name} {what}", data=data,
             actor_memory=f"I gave {{target}} {what}.", target_memory=f"{{actor}} gave me {what}.",
             witness_memory=f"{{actor}} handed {{target}} {what}.", importance=3)
        introduce(ctx.session, agent, target, ctx.tick)
        record_deed(ctx.session, target.id, agent, ctx.tick, f"gave me {what}", trust=0.15, affinity=0.2)
        nudge_reputation(agent, 0.02)
        return ActionResult(ok=True, socialised={agent.id, target.id})


IMPROVISE_USAGE = ('improvise {"what": "<anything a real person could do here this hour: cook, craft, '
                   'build, repair, teach, perform, preach, organise, invent, start a venture...>", '
                   '"uses": {"<item from your inventory>": 1}, "with": <ref of someone here or null>}'
                   '  -- the world decides how it turns out')


@register(ActionName.IMPROVISE, IMPROVISE_USAGE)
class Improvise:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        what = str(d.action_args.get("what", "")).strip()
        if not what:
            return "Say what you do in 'what'."
        uses = d.action_args.get("uses") or {}
        if not isinstance(uses, dict):
            return "'uses' must map items from your inventory to quantities."
        inv = agent.inventory or {}
        for item, qty in uses.items():
            try:
                q = int(qty)
            except (TypeError, ValueError):
                return f"How many {item}?"
            if q < 1 or inv.get(str(item).lower(), 0) < q:
                return f"You don't have {qty} {item} to use."
        if d.action_args.get("with") is not None:
            target = _person_here(ctx, agent, d.action_args.get("with"), perceived)
            if isinstance(target, str):
                return target
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        uses = {str(k).lower(): int(v) for k, v in (d.action_args.get("uses") or {}).items()}
        with_id = d.action_args.get("with")
        with_id = int(with_id) if with_id is not None else None
        ctx.improvised.append({"agent_id": agent.id, "what": str(d.action_args["what"]).strip()[:300],
                               "uses": uses, "with_id": with_id, "location_id": agent.location_id})
        return ActionResult(ok=True, socialised={agent.id, with_id} if with_id else set())


# --------------------------------------------------------------------------- democracy
PROPOSE_USAGE = ('propose_law {"text": "<your proposal in words>", "change": {"wage": "<job>", "to": 4.0} '
                 'or {"price": "<item>", "to": 2.5} or null}  -- at the Town Hall or Square; the town votes '
                 'for 12 hours and the majority decides')


@register(ActionName.PROPOSE_LAW, PROPOSE_USAGE)
class ProposeLaw:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.world import democracy

        here = _here(ctx, agent)
        if here is None or here.key not in democracy.VOTING_PLACES:
            return "Proposals are made at the Town Hall or the Town Square."
        text = str(d.action_args.get("text", "")).strip()
        if len(text) < 5:
            return "Write your proposal in 'text'."
        if any(p.author_id == agent.id for p in democracy.open_proposals(ctx.session)):
            return "You already have a proposal open; wait for the vote."
        parsed = democracy.parse_change(ctx.settings, d.action_args.get("change"))
        if isinstance(parsed, str):
            return parsed
        from asapville.world import economy

        boss = economy.owned_job(ctx.session, ctx.settings, parsed[1]) if parsed[0] == "wage" else None
        if boss is not None:
            return (f"The {parsed[1]} wage is set by {boss.name}, who owns that business; "
                    "the town can't vote on it. Bargain with them, or work elsewhere.")
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import democracy

        democracy.propose(ctx.session, ctx.settings, agent, str(d.action_args["text"]).strip(),
                          d.action_args.get("change"), ctx.tick, agent.location_id)
        return ActionResult(ok=True)


VOTE_USAGE = 'vote {"proposal": <number>, "choice": "yes" or "no"}  -- from anywhere, like a ballot slip'


@register(ActionName.VOTE, VOTE_USAGE)
class CastVote:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.db.models import Proposal

        try:
            p = ctx.session.get(Proposal, int(d.action_args.get("proposal")))
        except (TypeError, ValueError):
            p = None
        if p is None or p.status != "open":
            return "There's no open proposal with that number."
        if str(d.action_args.get("choice", "")).strip().lower() not in ("yes", "no"):
            return 'Choose "yes" or "no".'
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.db.models import Proposal
        from asapville.world import democracy

        p = ctx.session.get(Proposal, int(d.action_args["proposal"]))
        assert p is not None
        democracy.cast(ctx.session, agent, p,
                       str(d.action_args["choice"]).strip().lower() == "yes", ctx.tick, agent.location_id)
        return ActionResult(ok=True)


# --------------------------------------------------------------------------- wrongs
def _victim(ctx: TickContext, agent: Agent, ref: Any, perceived: set[int]) -> Agent | str:
    """Someone here, awake or asleep (sleepers are easy marks)."""
    try:
        target_id = int(ref)
    except (TypeError, ValueError):
        return "Give the ref of someone who is here."
    if target_id not in perceived or target_id == agent.id:
        return "There's nobody like that here."
    target = ctx.session.get(Agent, target_id)
    if target is None or not target.alive or target.location_id != agent.location_id \
            or target.travel_ticks_left > 0:
        return "They're not here any more."
    return target


def _onlookers(ctx: TickContext, agent: Agent, victim: Agent) -> list[Agent]:
    from sqlalchemy import select as _select

    return list(ctx.session.scalars(_select(Agent).where(
        Agent.location_id == agent.location_id, Agent.alive.is_(True), Agent.asleep.is_(False),
        Agent.travel_ticks_left == 0, Agent.id.not_in((agent.id, victim.id)))))


STEAL_USAGE = ('steal {"from": <ref of someone here>, "item": "<item>", "qty": 1}  or  '
               'steal {"from": <ref>, "asaps": 5}  -- take it without asking; you may be seen')
NOTICE_AWAKE, NOTICE_ASLEEP, NOTICE_WITNESS = 0.6, 0.15, 0.4


@register(ActionName.STEAL, STEAL_USAGE)
class Steal:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        victim = _victim(ctx, agent, d.action_args.get("from"), perceived)
        if isinstance(victim, str):
            return victim
        if "asaps" not in d.action_args and not str(d.action_args.get("item", "")).strip():
            return "Steal what? Name an item or an amount of asaps."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world.events import remember
        from asapville.world.social import describe

        victim = ctx.session.get(Agent, int(d.action_args["from"]))
        assert victim is not None
        if "asaps" in d.action_args:
            try:
                want = round(float(d.action_args["asaps"]) * 100)
            except (TypeError, ValueError):
                want = 0
            got = max(0, min(want, victim.money_cents))
            victim.money_cents -= got
            agent.money_cents += got
            what, data = f"{got / 100:.2f} asaps", {"cents": got}
        else:
            item = str(d.action_args["item"]).strip().lower()
            theirs = dict(victim.inventory or {})
            got = max(0, min(_qty(d), theirs.get(item, 0)))
            if got:
                theirs[item] -= got
                if theirs[item] <= 0:
                    del theirs[item]
                mine = dict(agent.inventory or {})
                mine[item] = mine.get(item, 0) + got
                victim.inventory, agent.inventory = theirs, mine
            what, data = f"{got} {item}", {"item": item, "qty": got}
        if not got:
            emit(ctx.session, tick=ctx.tick, kind="steal", actor=agent, target=victim,
                 location_id=agent.location_id, visibility="private",
                 summary=f"{agent.name} tried to rob {victim.name} but found nothing to take",
                 data={**data, "noticed": False, "empty": True},
                 actor_memory="I tried to rob {target}, but they had nothing like that on them.",
                 importance=3)
            return ActionResult(ok=True)
        noticed = ctx.rng.random() < (NOTICE_ASLEEP if victim.asleep else NOTICE_AWAKE)
        seen_by = [w for w in _onlookers(ctx, agent, victim) if ctx.rng.random() < NOTICE_WITNESS]
        ev = emit(ctx.session, tick=ctx.tick, kind="steal", actor=agent, target=victim,
                  location_id=agent.location_id, visibility="local" if noticed or seen_by else "private",
                  summary=f"{agent.name} stole {what} from {victim.name}"
                          + (" and was caught" if noticed else " unseen" if not seen_by else " (seen)"),
                  data={**data, "noticed": noticed, "witnesses": [w.id for w in seen_by]},
                  actor_memory=f"I stole {what} from {{target}}" + (" and they caught me at it." if noticed
                                                                    else "."),
                  target_memory=(f"{{actor}} stole {what} from me; I caught them at it." if noticed
                                 else f"I found {what} missing from my things; someone took it."),
                  importance=7 if noticed else 5)
        if noticed:
            record_deed(ctx.session, victim.id, agent, ctx.tick, f"stole {what} from me",
                        trust=-0.5, affinity=-0.4)
        for w in seen_by:
            remember(ctx.session, w.id, ctx.tick, "observation",
                     f"I saw {describe(ctx.session, w.id, agent)} steal {what} from "
                     f"{describe(ctx.session, w.id, victim)}.", importance=6, event_id=ev.id)
            record_deed(ctx.session, w.id, agent, ctx.tick,
                        f"stole from {victim.name.split()[0]} before my eyes", trust=-0.3, affinity=-0.15)
        if noticed or seen_by:
            nudge_reputation(agent, -0.1 * (1 + len(seen_by)))
        return ActionResult(ok=True)


ATTACK_USAGE = 'attack {"to": <ref of someone here>}  -- hit them; it hurts both of you, and people remember'
ATTACK_DAMAGE, ATTACK_COST = 12.0, 8.0


@register(ActionName.ATTACK, ATTACK_USAGE)
class Attack:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        victim = _victim(ctx, agent, d.action_args.get("to"), perceived)
        if isinstance(victim, str):
            return victim
        if agent.energy < ATTACK_COST:
            return "You are too exhausted to fight."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world.events import remember
        from asapville.world.social import describe

        victim = ctx.session.get(Agent, int(d.action_args["to"]))
        assert victim is not None
        victim.health = needs_rules.clamp(victim.health - ATTACK_DAMAGE)
        victim.mood = needs_rules.clamp(victim.mood - 15)
        agent.energy = needs_rules.clamp(agent.energy - ATTACK_COST)
        was_asleep = victim.asleep
        victim.asleep = False
        onlookers = _onlookers(ctx, agent, victim)
        ev = emit(ctx.session, tick=ctx.tick, kind="attack", actor=agent, target=victim,
                  location_id=agent.location_id, visibility="local",
                  summary=f"{agent.name} attacked {victim.name}" + (" in their sleep" if was_asleep else ""),
                  data={"damage": ATTACK_DAMAGE, "asleep": was_asleep},
                  actor_memory="I attacked {target}.",
                  target_memory="{actor} attacked me" + (" while I slept." if was_asleep else "."),
                  importance=8)
        introduce(ctx.session, agent, victim, ctx.tick)
        record_deed(ctx.session, victim.id, agent, ctx.tick, "attacked me", trust=-0.6, affinity=-0.6)
        for w in onlookers:
            remember(ctx.session, w.id, ctx.tick, "observation",
                     f"I saw {describe(ctx.session, w.id, agent)} attack "
                     f"{describe(ctx.session, w.id, victim)}.",
                     importance=7, event_id=ev.id)
            record_deed(ctx.session, w.id, agent, ctx.tick,
                        f"attacked {victim.name.split()[0]} before my eyes", trust=-0.3, affinity=-0.3)
        nudge_reputation(agent, -0.1 * (1 + len(onlookers)))
        return ActionResult(ok=True)


# --------------------------------------------------------------------------- property
BUY_PROPERTY_USAGE = ("buy_property {}  -- buy the place you stand in from the town, if it is for sale "
                      "(an empty shop, or a business such as the farm, mill or bakery); a business's "
                      "takings become yours, and so do its wages and prices")


@register(ActionName.BUY_PROPERTY, BUY_PROPERTY_USAGE)
class BuyProperty:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.world import economy

        here = _here(ctx, agent)
        if here is None or not economy.is_business(ctx.settings, here):
            return "This place isn't for sale. Empty shops and the town's businesses are."
        if here.owner_id is not None:
            owner = ctx.session.get(Agent, here.owner_id)
            return f"{here.name} already belongs to {owner.name if owner else 'someone'}."
        price = economy.business_price(ctx.settings, here)
        if agent.money_cents < price:
            return f"{here.name} costs {price / 100:.2f} asaps; you have {agent.money_cents / 100:.2f}."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import economy

        here = _here(ctx, agent)
        assert here is not None
        price = economy.business_price(ctx.settings, here)
        ws = ctx.session.get(WorldState, 1)
        assert ws is not None
        agent.money_cents -= price
        ws.treasury_cents += price
        here.owner_id = agent.id
        jobs = list(economy.jobs_at(ctx.settings, here.key))
        if here.kind == "shop" and not jobs and here.key not in ctx.settings.economy.shops:
            mine = "only I can trade there (open_stall)."
        else:
            mine = ("its takings are mine now, and I pay its wages and buy its materials from my own "
                    "money; I set the wage (hire) and the prices (set_price), and I can fire people.")
        emit(ctx.session, tick=ctx.tick, kind="property", actor=agent, location_id=here.id,
             visibility="public",
             summary=f"{agent.name} bought {here.name} for {price / 100:.2f}",
             data={"place": here.key, "cents": price, "jobs": jobs},
             actor_memory=f"I bought {here.name} from the town for {price / 100:.2f} asaps. It is mine; "
                          + mine,
             witness_memory=f"{{actor}} bought {here.name}" + (
                 f"; whoever works there as {' or '.join(jobs)} now works for {{actor}}." if jobs else "."),
             importance=7)
        for w in economy.workers_at(ctx.session, ctx.settings, here.key):
            if w.id != agent.id:
                remember(ctx.session, w.id, ctx.tick, "observation",
                         f"{agent.name} bought {here.name}; I work for them now, and they set my wage.",
                         importance=6)
        nudge_reputation(agent, 0.02)
        return ActionResult(ok=True)


def _my_business_for_job(ctx: TickContext, agent: Agent, job: str) -> Location | str:
    spec = ctx.settings.economy.jobs.get(job)
    if spec is None:
        return f"There is no job called '{job}'."
    place = ctx.by_key[spec.workplace]
    if place.owner_id != agent.id:
        return f"You don't own {place.name}; only its owner sets the {job} wage."
    return place


HIRE_USAGE = ('hire {"job": "<job at your business>", "wage": 4.0}  -- for business owners: set the '
              'hourly wage you pay; anyone may then apply')


@register(ActionName.HIRE, HIRE_USAGE)
class Hire:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        place = _my_business_for_job(ctx, agent, str(d.action_args.get("job", "")).strip().lower())
        if isinstance(place, str):
            return place
        try:
            wage = float(d.action_args.get("wage"))
        except (TypeError, ValueError):
            return 'Say the wage: "wage": <asaps per hour>.'
        if not 0 <= wage <= 50:
            return "A wage must be between 0 and 50 asaps an hour."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import economy

        job = str(d.action_args["job"]).strip().lower()
        place = _my_business_for_job(ctx, agent, job)
        assert not isinstance(place, str)
        cents = round(float(d.action_args["wage"]) * 100)
        ws = ctx.session.get(WorldState, 1)
        assert ws is not None
        old = economy.wage_of(ctx.session, ctx.settings, job)
        ws.wages = {**(ws.wages or {}), job: cents}
        text = f"{agent.name} is hiring at {place.name}: {job}, {cents / 100:.2f} asaps an hour"
        emit(ctx.session, tick=ctx.tick, kind="wages", actor=agent, location_id=agent.location_id,
             visibility="public", summary=f"{text} (was {old / 100:.2f})",
             data={"job": job, "from": old, "to": cents, "owner": agent.id,
                   "notice": f"{text}. Apply at {place.name}."},
             actor_memory=f"I set the {job} wage at my {place.name} to {cents / 100:.2f} an hour "
                          f"(it was {old / 100:.2f}).", importance=4)
        for w in economy.workers_in(ctx.session, job):
            if w.id != agent.id:
                remember(ctx.session, w.id, ctx.tick, "observation",
                         f"{agent.name}, who owns {place.name}, changed my {job} wage from "
                         f"{old / 100:.2f} to {cents / 100:.2f} an hour.", importance=5)
                record_deed(ctx.session, w.id, agent, ctx.tick,
                            "raised my wage" if cents > old else "cut my wage",
                            trust=0.1 if cents > old else -0.1 if cents < old else 0.0)
        return ActionResult(ok=True)


FIRE_USAGE = 'fire {"who": <id>}  -- for business owners: dismiss someone who works at your business'


@register(ActionName.FIRE, FIRE_USAGE)
class Fire:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        try:
            who = ctx.session.get(Agent, int(d.action_args.get("who")))
        except (TypeError, ValueError):
            who = None
        if who is None or not who.alive:
            return "Name the worker by their id: \"who\": <id>."
        spec = ctx.settings.economy.jobs.get(who.role)
        if spec is None or ctx.by_key[spec.workplace].owner_id != agent.id:
            return f"{who.name} doesn't work at a business you own."
        if who.id == agent.id:
            return "To stop working at your own business, use quit."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        who = ctx.session.get(Agent, int(d.action_args["who"]))
        assert who is not None
        job = who.role
        place = ctx.by_key[ctx.settings.economy.jobs[job].workplace]
        who.role = "unemployed"
        emit(ctx.session, tick=ctx.tick, kind="fired", actor=agent, target=who,
             location_id=agent.location_id, visibility="local",
             summary=f"{agent.name} fired {who.name} from {place.name}",
             data={"job": job, "place": place.key},
             actor_memory=f"I fired {who.name} from my {place.name}.",
             target_memory=f"{agent.name} fired me from {place.name}; I am no longer a {job}.",
             witness_memory="{actor} dismissed {target} from their work.", importance=6)
        record_deed(ctx.session, who.id, agent, ctx.tick, "fired me", trust=-0.3, affinity=-0.3)
        return ActionResult(ok=True)


SET_PRICE_USAGE = ('set_price {"item": "<item>", "to": 2.5}  -- for business owners: the price of '
                   'something your business sells or makes')


@register(ActionName.SET_PRICE, SET_PRICE_USAGE)
class SetPrice:
    @staticmethod
    def _place(ctx: TickContext, agent: Agent, item: str) -> Location | None:
        from asapville.world import economy

        for loc in ctx.by_key.values():
            if loc.owner_id != agent.id:
                continue
            made = {i for job in economy.jobs_at(ctx.settings, loc.key).values() for i in job.makes}
            if item in economy.shop_items(ctx.settings, loc.key) or item in made:
                return loc
        return None

    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        item = str(d.action_args.get("item", "")).strip().lower()
        if item in ctx.settings.economy.unlimited:
            return f"The price of {item} is the town's."
        if SetPrice._place(ctx, agent, item) is None:
            return f"You own no business that sells or makes {item or 'that'}."
        try:
            to = float(d.action_args.get("to"))
        except (TypeError, ValueError):
            return 'Say the price: "to": <asaps>.'
        if not 0.1 <= to <= 100:
            return "A price must be between 0.10 and 100 asaps."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import economy

        item = str(d.action_args["item"]).strip().lower()
        place = SetPrice._place(ctx, agent, item)
        assert place is not None
        cents = round(float(d.action_args["to"]) * 100)
        old = economy.price_of(ctx.settings, item, ctx.session, at=place.key) or 0
        ws = ctx.session.get(WorldState, 1)
        assert ws is not None
        ws.prices = {**(ws.prices or {}), f"{place.key}:{item}": cents}
        text = f"{place.name} now charges {cents / 100:.2f} for {item} (was {old / 100:.2f})"
        emit(ctx.session, tick=ctx.tick, kind="notice", actor=agent, location_id=agent.location_id,
             visibility="public", summary=f"{agent.name}: {text}",
             data={"text": text, "author_id": agent.id, "item": item, "place": place.key, "cents": cents},
             actor_memory=f"I set the price of {item} at my {place.name} to {cents / 100:.2f}.",
             importance=4)
        return ActionResult(ok=True)


# --------------------------------------------------------------------------- AsapBank
def _asaps(d: Decision) -> int | None:
    try:
        cents = round(float(d.action_args.get("asaps")) * 100)
    except (TypeError, ValueError):
        return None
    return cents if cents > 0 else None


def _bank_check(ctx: TickContext, agent: Agent, d: Decision) -> tuple[int | None, str | None]:
    here = _here(ctx, agent)
    if here is None or here.kind != "bank":
        return None, "Banking is done at AsapBank; go there first."
    cents = _asaps(d)
    if cents is None:
        return None, 'Say how much: "asaps": <amount>.'
    return cents, None


@register(ActionName.DEPOSIT, 'deposit {"asaps": 20}  -- at AsapBank: put money in savings; it earns '
                              'interest every day and is safe from thieves')
class Deposit:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        cents, err = _bank_check(ctx, agent, d)
        if err:
            return err
        if cents > agent.money_cents:
            return f"You only have {agent.money_cents / 100:.2f} asaps on you."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import bank

        cents = _asaps(d)
        bank.deposit(ctx.session, agent, cents, ctx.tick)
        acc = bank.account(ctx.session, agent, ctx.tick)
        emit(ctx.session, tick=ctx.tick, kind="bank", actor=agent, location_id=agent.location_id,
             summary=f"{agent.name} deposited {cents / 100:.2f} at AsapBank",
             data={"op": "deposit", "cents": cents},
             actor_memory=f"I put {cents / 100:.2f} asaps in my AsapBank savings "
                          f"(now {acc.deposit_cents / 100:.2f}).", importance=3)
        return ActionResult(ok=True)


@register(ActionName.WITHDRAW, 'withdraw {"asaps": 20}  -- at AsapBank: take money out of your savings')
class Withdraw:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.world import bank

        cents, err = _bank_check(ctx, agent, d)
        if err:
            return err
        acc = bank.account(ctx.session, agent, ctx.tick, create=False)
        have = acc.deposit_cents if acc else 0
        if cents > have:
            return f"Your savings hold {have / 100:.2f} asaps."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import bank

        cents = _asaps(d)
        bank.withdraw(ctx.session, agent, cents, ctx.tick)
        emit(ctx.session, tick=ctx.tick, kind="bank", actor=agent, location_id=agent.location_id,
             summary=f"{agent.name} withdrew {cents / 100:.2f} from AsapBank",
             data={"op": "withdraw", "cents": cents},
             actor_memory=f"I took {cents / 100:.2f} asaps out of my AsapBank savings.", importance=3)
        return ActionResult(ok=True)


@register(ActionName.BORROW, 'borrow {"asaps": 100}  -- at AsapBank: take a loan up to your credit limit; '
                             'it costs interest every day and must be repaid within a week, or the bank '
                             'takes your savings, cash and businesses')
class Borrow:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.world import bank

        cents, err = _bank_check(ctx, agent, d)
        if err:
            return err
        room = bank.can_borrow(ctx.session, ctx.settings, agent, ctx.tick)
        if cents > room:
            return (f"AsapBank will lend you at most {room / 100:.2f} asaps more"
                    + (" (no job, no recent wages, no business to secure it)" if room == 0 else "") + ".")
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import bank

        cents = _asaps(d)
        bank.borrow(ctx.session, ctx.settings, agent, cents, ctx.tick)
        acc = bank.account(ctx.session, agent, ctx.tick)
        cfg = ctx.settings.bank
        emit(ctx.session, tick=ctx.tick, kind="bank", actor=agent, location_id=agent.location_id,
             summary=f"{agent.name} borrowed {cents / 100:.2f} from AsapBank",
             data={"op": "borrow", "cents": cents, "owed": acc.loan_cents, "due": acc.loan_due_tick},
             actor_memory=f"I borrowed {cents / 100:.2f} asaps from AsapBank. I owe "
                          f"{acc.loan_cents / 100:.2f}, "
                          f"growing {cfg.loan_rate:.0%} a day, due within {cfg.loan_days} days; if I don't "
                          "repay, the bank takes my savings, my cash and my businesses.", importance=8)
        return ActionResult(ok=True)


@register(ActionName.REPAY, 'repay {"asaps": 50}  -- at AsapBank: pay back (part of) your loan')
class Repay:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.world import bank

        cents, err = _bank_check(ctx, agent, d)
        if err:
            return err
        acc = bank.account(ctx.session, agent, ctx.tick, create=False)
        if acc is None or acc.loan_cents == 0:
            return "You owe AsapBank nothing."
        if min(cents, acc.loan_cents) > agent.money_cents:
            return f"You only have {agent.money_cents / 100:.2f} asaps on you."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import bank

        paid = bank.repay(ctx.session, agent, _asaps(d), ctx.tick)
        acc = bank.account(ctx.session, agent, ctx.tick)
        left = acc.loan_cents
        emit(ctx.session, tick=ctx.tick, kind="bank", actor=agent, location_id=agent.location_id,
             summary=f"{agent.name} repaid {paid / 100:.2f} to AsapBank",
             data={"op": "repay", "cents": paid, "owed": left},
             actor_memory=(f"I repaid {paid / 100:.2f} asaps to AsapBank; "
                           + (f"I still owe {left / 100:.2f}." if left else "my loan is cleared.")),
             importance=6 if not left else 4)
        if not left:
            nudge_reputation(agent, 0.03)
        return ActionResult(ok=True)


# --------------------------------------------------------------------------- cooking
def has_kitchen(agent: Agent, here: Location | None) -> bool:
    """A citizen cooks at home, or in a shop or business of their own."""
    return here is not None and (here.id == agent.home_id or here.owner_id == agent.id)


def cookable(ctx: TickContext, agent: Agent) -> list[str]:
    inv = agent.inventory or {}
    return [dish for dish, r in ctx.settings.recipes.items()
            if all(inv.get(i, 0) >= q for i, q in r.uses.items())]


@register(ActionName.COOK, 'cook {"dish": "<dish>"}  -- at home, or in a shop or business you own: turn '
                           'ingredients you carry into a dish (eat it, give it, or sell it at your stall)')
class Cook:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        dish = str(d.action_args.get("dish", "")).strip().lower().replace(" ", "_")
        recipe = ctx.settings.recipes.get(dish)
        if recipe is None:
            return f"Nobody knows how to cook '{dish}'. Dishes: {', '.join(ctx.settings.recipes)}."
        if not has_kitchen(agent, _here(ctx, agent)):
            return "You need a kitchen: your home, or a shop or business you own."
        inv = agent.inventory or {}
        missing = [f"{q} {i}" for i, q in recipe.uses.items() if inv.get(i, 0) < q]
        if missing:
            return f"{dish} needs {', '.join(f'{q} {i}' for i, q in recipe.uses.items())}; you lack " \
                   + ", ".join(missing) + "."
        if agent.energy < 4:
            return "You are too tired to cook."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        dish = str(d.action_args["dish"]).strip().lower().replace(" ", "_")
        recipe = ctx.settings.recipes[dish]
        inv = dict(agent.inventory or {})
        for item, qty in recipe.uses.items():
            inv[item] -= qty
            if inv[item] <= 0:
                del inv[item]
        inv[dish] = inv.get(dish, 0) + recipe.makes
        agent.inventory = inv
        agent.energy = needs_rules.clamp(agent.energy - 4)
        used = ", ".join(f"{q} {i}" for i, q in recipe.uses.items())
        emit(ctx.session, tick=ctx.tick, kind="cook", actor=agent, location_id=agent.location_id,
             summary=f"{agent.name} cooked {recipe.makes} {dish.replace('_', ' ')} from {used}",
             data={"dish": dish, "made": recipe.makes, "used": recipe.uses},
             actor_memory=f"I cooked {recipe.makes} {dish.replace('_', ' ')} from {used}.",
             witness_memory=f"{{actor}} cooked {dish.replace('_', ' ')}; it smelled good.", importance=3)
        return ActionResult(ok=True)


# --------------------------------------------------------------------------- the council
STAND_USAGE = ('stand {"pitch": "<why the town should elect you>"}  -- at the Town Hall or Square while a '
               'council election is open: become a candidate')


@register(ActionName.STAND, STAND_USAGE)
class Stand:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.db.models import Candidacy
        from asapville.world import democracy, elections

        e = elections.open_election(ctx.session)
        if e is None:
            return "No council election is open now."
        here = _here(ctx, agent)
        if here is None or here.key not in democracy.VOTING_PLACES:
            return "Candidates declare at the Town Hall or the Town Square."
        if ctx.session.scalar(select(Candidacy).where(Candidacy.election_id == e.id,
                                                      Candidacy.agent_id == agent.id)) is not None:
            return "You are already standing in this election."
        if len(str(d.action_args.get("pitch", "")).strip()) < 5:
            return "Say why the town should elect you, in 'pitch'."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import elections

        e = elections.open_election(ctx.session)
        assert e is not None
        elections.stand(ctx.session, e, agent, str(d.action_args["pitch"]).strip(), ctx.tick,
                        agent.location_id)
        return ActionResult(ok=True)


@register(ActionName.ELECT, 'elect {"candidate": <ref>}  -- from anywhere: your one ballot for the council')
class Elect:
    @staticmethod
    def validate(ctx: TickContext, agent: Agent, d: Decision, perceived: set[int]) -> str | None:
        from asapville.db.models import Candidacy
        from asapville.world import elections

        e = elections.open_election(ctx.session)
        if e is None:
            return "No council election is open now."
        try:
            ref = int(d.action_args.get("candidate"))
        except (TypeError, ValueError):
            return 'Name a candidate by ref: elect {"candidate": <ref>}.'
        if ctx.session.scalar(select(Candidacy).where(Candidacy.election_id == e.id,
                                                      Candidacy.agent_id == ref)) is None:
            return "That person is not standing in this election."
        return None

    @staticmethod
    def execute(ctx: TickContext, agent: Agent, d: Decision) -> ActionResult:
        from asapville.world import elections

        e = elections.open_election(ctx.session)
        candidate = ctx.session.get(Agent, int(d.action_args["candidate"]))
        assert e is not None and candidate is not None
        elections.cast(ctx.session, e, agent, candidate, ctx.tick)
        return ActionResult(ok=True)
