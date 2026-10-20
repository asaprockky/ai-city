"""Perception: what one agent can sense right now. Built from its surroundings and
its own memories only (no omniscience)."""

from __future__ import annotations

from sqlalchemy import select

from asapville.agents.schemas import Destination, JobHere, Notice, Perception, SeenAgent, ShopItem
from asapville.db.models import Agent, Event, Memory, Stall
from asapville.world import bank, economy
from asapville.world.actions import TickContext, available_usages
from asapville.world.map import travel_ticks
from asapville.world.needs import describe_needs, sick_of
from asapville.world.social import describe, knows_name, looks_of, standing

_DOING = {
    "rest": "resting", "eat": "eating", "talk": "talking", "forage": "searching for food",
    "idle": "standing around", "move": "just arrived", "sleep": "asleep", "work": "working",
    "buy": "buying something", "sell": "selling something", "apply": "asking for work",
    "write_notice": "writing a notice",
}

RECENT_TICKS = 3
PLAN_DAY_START = 5  # hours after midnight when a new day of plans begins


def _ownership(ctx: TickContext, agent: Agent, loc) -> str | None:
    cfg = ctx.settings
    if not economy.is_business(cfg, loc):
        return None
    price = economy.business_price(cfg, loc) / 100
    shop_only = loc.kind == "shop" and not economy.jobs_at(cfg, loc.key) and loc.key not in cfg.economy.shops
    if loc.owner_id is None:
        if shop_only:
            return f"empty shop for sale, {price:.0f} asaps"
        return f"run by the town; for sale, {price:.0f} asaps (buy_property there)"
    if loc.owner_id == agent.id:
        return "your own shop" if shop_only else "your own business"
    owner = ctx.session.get(Agent, loc.owner_id)
    who = describe(ctx.session, agent.id, owner) if owner else "someone"
    return f"shop owned by {who}" if shop_only else f"business owned by {who}"


def _kitchen(ctx: TickContext, agent: Agent, here) -> str | None:
    from asapville.world.actions import cookable, has_kitchen

    cfg = ctx.settings
    can = cookable(ctx, agent)
    if has_kitchen(agent, here):
        menu = "; ".join(f"{d.replace('_', ' ')} ({', '.join(f'{q} {i}' for i, q in r.uses.items())} -> "
                         f"{r.makes})" for d, r in cfg.recipes.items())
        now = f" With what you carry you can cook now: {', '.join(can)}." if can else ""
        return (f"You have a kitchen here (cook). Recipes: {menu}.{now} Ingredients are sold at the "
                "Market, the farm gate and the Bakery; dishes can be eaten, given, or sold at a stall "
                "you open (open_stall), at the price you choose.")
    if can:
        return (f"With what you carry you could cook {', '.join(can)} at home or in a shop you own.")
    return None


def todays_plan(ctx: TickContext, agent: Agent) -> str | None:
    """The plan this citizen wrote this simulated day, if any."""
    from asapville.world.clock import Clock

    m = ctx.session.scalar(select(Memory).where(Memory.agent_id == agent.id, Memory.kind == "plan")
                           .order_by(Memory.created_at_tick.desc(), Memory.id.desc()).limit(1))
    if m is None:
        return None
    # a "day" for plans starts at 05:00, so night owls don't plan tomorrow at midnight
    shift = lambda t: Clock(ctx.settings.clock).at(t - PLAN_DAY_START).day  # noqa: E731
    return m.content if shift(m.created_at_tick) == shift(ctx.tick) else None


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
                    sells=economy.shop_items(cfg, loc.key) + sorted(
                        {st.item for st in economy.stalls_at(s, loc.id)
                         if st.qty > 0 and st.owner_id != agent.id}),
                    jobs=list(economy.jobs_at(cfg, loc.key)), note=_ownership(ctx, agent, loc))
        for loc in sorted(ctx.locations.values(), key=lambda x: x.id)
        if loc.id != here.id and (loc.kind != "home" or loc.id == agent.home_id)
    ]
    shop = [ShopItem(item=item, price_cents=economy.price_of(cfg, item, s, at=here.key) or 0,
                     qty=None if item in cfg.economy.unlimited else economy.stock_qty(s, here.id, item))
            for item in economy.shop_items(cfg, here.key)]
    for st in economy.stalls_at(s, here.id):
        if st.owner_id == agent.id:
            continue
        owner = s.get(Agent, st.owner_id)
        who = describe(s, agent.id, owner) if owner else "someone"
        shop.append(ShopItem(item=st.item, price_cents=st.price_cents, qty=st.qty,
                             seller=f'"{st.name}", run by {who}'))
    mine = []
    for st in s.scalars(select(Stall).where(Stall.owner_id == agent.id).order_by(Stall.id)):
        place = ctx.locations[st.location_id].name
        mine.append(f'"{st.name}" at {place}: {st.item} at {st.price_cents / 100:.2f} asaps, '
                    f'{st.qty} on the stall, {st.sold} sold so far')
    for loc in sorted(ctx.locations.values(), key=lambda x: x.id):
        if loc.owner_id != agent.id or not economy.is_business(cfg, loc):
            continue
        parts = []
        for name in economy.jobs_at(cfg, loc.key):
            n = len(economy.workers_in(s, name))
            parts.append(f"you pay {name}s {economy.wage_of(s, cfg, name) / 100:.2f}/h, {n} on the books")
        made = {i for job in economy.jobs_at(cfg, loc.key).values() for i in job.makes}
        for item in sorted(set(economy.shop_items(cfg, loc.key)) | made):
            price = economy.price_of(cfg, item, s, at=loc.key) or 0
            parts.append(f"{item} at {price / 100:.2f} ({economy.stock_qty(s, loc.id, item)} in stock)")
        if parts:
            mine.append(f"{loc.name} (its takings, wages and materials are your own money): "
                        + ", ".join(parts) + "; change them with hire / set_price, dismiss with fire")
    jobs_here = [JobHere(job=name, wage_cents=economy.wage_of(s, cfg, name),
                         workers=len(economy.workers_in(s, name)),
                         active=economy.active_workers(s, name, ctx.tick - 24))
                 for name, job in economy.jobs_at(cfg, here.key).items()]
    my_job = cfg.economy.jobs.get(agent.role)
    notices = []
    for ev in s.scalars(select(Event).where(Event.kind.in_(("notice", "wages", "law")),
                                            Event.tick >= ctx.tick - cfg.economy.notice_ticks)
                        .order_by(Event.id.desc()).limit(5)):
        author_id = ev.data.get("author_id")
        author = ev.data.get("author") or "someone"
        if author_id:
            author_agent = s.get(Agent, author_id)
            author = describe(s, agent.id, author_agent) if author_agent else author
        notices.append(Notice(text=ev.data.get("notice") or ev.data.get("text", ev.summary), author=author,
                              hours_ago=ctx.tick - ev.tick))
    plan = todays_plan(ctx, agent)
    from asapville.db.models import Vote
    from asapville.world import democracy

    proposals = []
    for pr in democracy.open_proposals(s):
        author = s.get(Agent, pr.author_id)
        my_vote = s.scalar(select(Vote).where(Vote.proposal_id == pr.id, Vote.agent_id == agent.id))
        voted = f"; you voted {'yes' if my_vote.yes else 'no'}" if my_vote else "; you haven't voted"
        by = describe(s, agent.id, author) if author else "someone"
        proposals.append(f"{democracy.describe(pr)} by {by}"
                         f": {pr.yes} yes, {pr.no} no, closes in {max(0, pr.closes_tick - ctx.tick)}h{voted}")
    decisions = [f"{democracy.describe(pr)}: {pr.status} {pr.yes}-{pr.no}"
                 for pr in democracy.recent_decisions(s)]
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
        location_note=_ownership(ctx, agent, here),
        is_home=here.id == agent.home_id,
        here=seen,
        destinations=dests,
        body=describe_needs(agent.hunger, agent.energy, agent.social, agent.health)
        + ([f"you are sick of eating {bored}"] if (bored := sick_of(list(agent.diet or []))) else []),
        hunger=round(agent.hunger),
        energy=round(agent.energy),
        health=round(agent.health),
        social=round(agent.social),
        money_cents=agent.money_cents,
        inventory=dict(agent.inventory or {}),
        notices=notices,
        job=agent.role if my_job else None,
        workplace=ctx.by_key[my_job.workplace].name if my_job else None,
        wage_cents=economy.wage_of(s, cfg, agent.role) if my_job else None,
        job_output_cents=(sum((economy.price_of(cfg, item, s, at=my_job.workplace) or 0) * qty
                              for item, qty in my_job.makes.items()) if my_job else None),
        employer=(boss.name if (boss := economy.owned_job(s, cfg, agent.role)) and boss.id != agent.id
                  else None),
        shop=shop,
        jobs_here=jobs_here,
        plan=plan,
        businesses=mine,
        bank=bank.describe(s, cfg, agent, ctx.tick),
        kitchen=_kitchen(ctx, agent, here),
        bank_offer=(f"AsapBank: deposit savings (they earn {cfg.bank.deposit_rate:.1%} a day), withdraw, "
                    f"borrow up to {bank.can_borrow(s, cfg, agent, ctx.tick) / 100:.2f} asaps more "
                    f"({cfg.bank.loan_rate:.0%} a day, due in {cfg.bank.loan_days} days), or repay"
                    if here.kind == "bank" else None),
        proposals=proposals,
        decisions=decisions,
        people=standing(s, agent),
        recent_dialogue=[m.content for m in recent if m.kind == "dialogue"][-8:],
        recent_events=[m.content for m in recent if m.kind in ("observation", "outcome", "action")][-8:],
        available_actions=available_usages(),
    )


def render_perception(p: Perception) -> str:
    """Perception as second-person prose for the dynamic part of the prompt."""
    lines = [
        f"It is {p.time_label} ({p.part_of_day}). Season: {p.season}. Weather: {p.weather}.",
        f"You are at {p.location_name} [{p.location_key}]" + (" — your home." if p.is_home else ".")
        + (f" ({p.location_note}.)" if p.location_note else ""),
    ]
    if p.here:
        lines.append("People here (use ref to address them):")
        for a in p.here:
            who = a.name or f"a stranger ({a.looks})"
            state = f" — {a.doing}" if a.doing else ""
            lines.append(f"  - ref {a.ref}: {who}{state}")
    else:
        lines.append("Nobody else is here.")
    # Every gauge reads the same way (higher is better): citizens read a raw "hunger 4" as an empty
    # stomach and "hunger 89" as fed, the opposite of what it meant, so fullness is shown instead.
    lines.append(f"Your body (each out of 100, higher is better): fed {100 - p.hunger}, energy {p.energy}, "
                 f"health {p.health}, company {p.social}."
                 + (" " + "; ".join(p.body).capitalize() + "." if p.body else ""))
    asaps = p.money_cents / 100
    inv = ", ".join(f"{k} x{v}" for k, v in sorted(p.inventory.items())) or "nothing"
    lines.append(f"You carry {asaps:.2f} asaps and: {inv}.")
    if p.bank:
        lines.append(p.bank)
    if p.bank_offer:
        lines.append(p.bank_offer + ".")
    if p.kitchen:
        lines.append(p.kitchen)
    if p.job:
        boss = f" for {p.employer}, who owns it" if p.employer else ""
        lines.append(f"Your job: {p.job} at {p.workplace}{boss}, {(p.wage_cents or 0) / 100:.2f} asaps per "
                     f"hour worked (choose 'work' while you are there).")
        if p.job_output_cents and p.wage_cents is not None and p.job_output_cents >= 2 * p.wage_cents:
            seller = p.employer or "the town"
            how = (f"{p.employer} sets your wage: bargain with them, or work elsewhere." if p.employer else
                   "Wages are not fixed: any citizen may put a new wage to a town vote "
                   "(propose_law at the Town Hall or Square).")
            lines.append(f"One hour of your work makes goods {seller} sells for "
                         f"{p.job_output_cents / 100:.2f} asaps, and pays you {p.wage_cents / 100:.2f}. "
                         + how)
    else:
        lines.append("You have no job.")
    town = [x for x in p.shop if x.seller is None]
    stalls = [x for x in p.shop if x.seller is not None]
    if town:
        goods = ", ".join(f"{x.item} {x.price_cents / 100:.2f} asaps"
                          + (" (always available)" if x.qty is None else f" ({x.qty} left)")
                          for x in town)
        lines.append(f"For sale here: {goods}.")
    if stalls:
        lines.append("Citizens' stalls here: " + "; ".join(
            f"{x.item} {x.price_cents / 100:.2f} asaps ({x.qty} left) at {x.seller}" for x in stalls)
            + '. Add "from": "<stall name>" to buy from one.')
    if p.businesses:
        lines.append("Your own businesses: " + "; ".join(p.businesses) + ".")
    if p.jobs_here:
        others = [j for j in p.jobs_here if j.job != p.job]
        lines.append("Jobs here: " + "; ".join(
            f"{j.job} ({j.wage_cents / 100:.2f}/h, {j.workers} on the books"
            + (f", {j.active} turned up in the last day"
               if j.active is not None and j.active < j.workers else "")
            + (", this is YOUR job: choose 'work' to do a shift)" if j.job == p.job else ")")
            for j in p.jobs_here) + (". Use 'apply' to take another." if others else "."))
    lines.append("Places you can walk to (key: name, hours on foot):")
    for d in p.destinations:
        extra = []
        if d.sells:
            extra.append("sells " + ", ".join(d.sells))
        if d.jobs:
            extra.append("hires " + ", ".join(d.jobs))
        if d.note:
            extra.append(d.note)
        tail = " — " + "; ".join(extra) if extra else ""
        lines.append(f"  - {d.key}: {d.name} ({d.travel_hours}h){tail}")
    if p.people:
        lines.append("People you know and what they have done to you:")
        lines.extend(f"  - {x}" for x in p.people)
    if p.plan:
        lines.append(f"Your own plan for today (you wrote it this morning): {p.plan}")
    if p.hunger < 50 and p.energy > 35 and p.health > 40:
        lines.append("Your body is fine right now: this hour is yours. Follow your plan, chase what "
                     "you want from life, or try something new of your own (improvise). Nobody "
                     "decides for you.")
    if p.proposals:
        lines.append("Open proposals (the town decides by majority; vote at the Town Hall or Square): "
                     + " | ".join(p.proposals))
    if p.decisions:
        lines.append("Recently decided by the town: " + " | ".join(p.decisions))
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

