"""Command line: init-world, run, pause, resume, report, personas."""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker

from asapville.agents.birth import birth_agent, roll_founders
from asapville.agents.persona import persona_block, unpleasant_reasons
from asapville.agents.schemas import Personality
from asapville.config import Settings, load_settings
from asapville.db.models import Agent, Brand, Event, LLMCall, Location, WorldState
from asapville.db.session import make_engine, make_session_factory, upgrade_to_head
from asapville.engine import World, get_world_state
from asapville.llm.budget import BudgetGuard
from asapville.llm.client import LLMGateway, make_backend
from asapville.llm.routing import TIER_NAMES
from asapville.world.clock import Clock
from asapville.world.map import seed_locations

log = logging.getLogger("asapville.cli")

FOUNDING_BRANDS = [
    {"name": "AsapBank", "category": "bank",
     "logo_description": "A plain teal hexagon holding a single white upward chevron; "
                         "lettering in a narrow slab serif."},
]


def _bootstrap(settings: Settings) -> tuple[sessionmaker[Session], LLMGateway]:
    engine = make_engine(settings.db_url)
    sessions = make_session_factory(engine)
    guard = BudgetGuard(settings.budget, opus_model=settings.llm.models.opus)
    gateway = LLMGateway(make_backend(settings.llm), settings.llm, guard)
    return sessions, gateway


async def init_world(settings: Settings, *, population: int | None = None,
                     sessions: sessionmaker[Session] | None = None,
                     gateway: LLMGateway | None = None) -> list[Agent]:
    """Migrate the DB, create map, founding brands and the first citizens."""
    upgrade_to_head(settings.db_url)
    if sessions is None or gateway is None:
        sessions, gateway = _bootstrap(settings)
    with sessions() as s:
        gateway.guard.load(s)
        if s.get(WorldState, 1) is not None:
            raise SystemExit("world already initialised (delete the database to start over)")
        s.add(WorldState(id=1, tick=0, seed=settings.world.seed))
        seed_locations(s, settings.map)
        for b in FOUNDING_BRANDS:
            s.add(Brand(**b, founded_tick=0, reputation=0.0))
        homes = list(s.scalars(select(Location).where(Location.kind == "home").order_by(Location.id)))
        rng = random.Random(settings.world.seed)
        n = population if population is not None else settings.world.population
        agents = []
        for i, seed in enumerate(roll_founders(rng, settings, n)):
            home = homes[i] if i < len(homes) else None  # extra citizens start homeless
            agents.append(await birth_agent(s, gateway, settings, rng, seed_index=i, home=home,
                                            seed=seed))
            log.info("born: %s (%s)", agents[-1].name, seed.moral_alignment)
            if home is not None:
                home.owner_id = agents[-1].id
        gateway.flush(s)
        s.commit()
        return agents


def cost_report(session: Session) -> str:
    rows = session.execute(
        select(LLMCall.tier, LLMCall.model, LLMCall.backend, func.count(LLMCall.id),
               func.sum(LLMCall.input_tokens), func.sum(LLMCall.output_tokens),
               func.sum(LLMCall.cache_read_tokens), func.sum(LLMCall.cache_write_tokens),
               func.sum(LLMCall.cost_usd), func.sum(case((LLMCall.ok.is_(False), 1), else_=0)))
        .group_by(LLMCall.tier, LLMCall.model, LLMCall.backend).order_by(LLMCall.tier)
    ).all()
    lines = ["COST REPORT (from llm_calls)",
             f"{'tier':<8} {'model':<20} {'backend':<9} {'calls':>6} {'in':>9} {'out':>8} "
             f"{'c.read':>9} {'c.write':>9} {'hit%':>5} {'fail':>4} {'usd':>9}"]
    total = 0.0
    for tier, model, backend, n, i, o, cr, cw, usd, fails in rows:
        i, o, cr, cw, usd = i or 0, o or 0, cr or 0, cw or 0, usd or 0.0
        hit = 100.0 * cr / (cr + cw + i) if (cr + cw + i) else 0.0
        total += usd
        lines.append(f"{TIER_NAMES.get(tier, tier):<8} {model:<20} {backend:<9} {n:>6} {i:>9} {o:>8} "
                     f"{cr:>9} {cw:>9} {hit:>5.1f} {fails or 0:>4} {usd:>9.4f}")
    lines.append(f"TOTAL SPEND: ${total:.4f}")
    return "\n".join(lines)


def world_report(session: Session, settings: Settings, *, events: int = 15) -> str:
    ws = get_world_state(session)
    t = Clock(settings.clock).at(ws.tick)
    locs = {loc.id: loc for loc in session.scalars(select(Location))}
    lines = [f"WORLD: {t.label()} | tick {ws.tick} | mode {ws.mode} | "
             f"{'PAUSED' if ws.paused else 'running'} | weather {ws.weather}", "",
             f"{'id':>3} {'name':<16} {'tier':<8} {'where':<22} {'hun':>4} {'nrg':>4} {'hp':>4} "
             f"{'asaps':>8} {'state':<8} mood"]
    for a in session.scalars(select(Agent).order_by(Agent.id)):
        where = locs[a.location_id].name if a.location_id else (
            f"-> {locs[a.destination_id].name}" if a.destination_id else "?")
        state = "dead" if not a.alive else ("asleep" if a.asleep else "awake")
        lines.append(f"{a.id:>3} {a.name[:16]:<16} {TIER_NAMES[a.tier]:<8} {where[:22]:<22} "
                     f"{a.hunger:>4.0f} {a.energy:>4.0f} {a.health:>4.0f} "
                     f"{a.money_cents / 100:>8.2f} {state:<8} {a.mood_label}")
    lines += ["", f"LAST {events} EVENTS"]
    recent = list(session.scalars(select(Event).where(Event.visibility != "system")
                                  .order_by(Event.id.desc()).limit(events)))
    for e in reversed(recent):
        lines.append(f"  [t{e.tick:>4}] {e.kind:<14} {e.summary}")
    return "\n".join(lines)


def persona_cards(session: Session) -> str:
    out = []
    for a in session.scalars(select(Agent).order_by(Agent.id)):
        p = Personality.model_validate(a.personality)
        out.append(f"=== #{a.id} {a.name} | {TIER_NAMES[a.tier]} | {p.moral_alignment} | "
                   f"{a.virtual_mac} {a.virtual_ip} ===")
        out.append(persona_block(a.name, p, age=a.seed_card.get("age")))
        out.append("")
    return "\n".join(out)


def _bar(v: float) -> str:
    filled = round(v * 10)
    return "█" * filled + "░" * (10 - filled) + f" {v:.2f}"


def persona_cards_markdown(session: Session, settings: Settings) -> str:
    """Observer-facing persona cards (seed facts + the agent's self-written identity)."""
    from asapville.llm.routing import model_for

    agents = list(session.scalars(select(Agent).order_by(Agent.id)))
    out = ["# Asapville — persona cards", ""]
    flagged = []
    for a in agents:
        p = Personality.model_validate(a.personality)
        seed = a.seed_card
        bad = unpleasant_reasons(p)
        if bad:
            flagged.append(f"**{a.name}** ({', '.join(bad)})")
        ap = p.appearance
        out += [
            f"## {a.name}" + ("  ⚠ unpleasant" if bad else ""),
            "",
            f"*{ap.self_description}*",
            "",
            f"- **Age** {seed.get('age')} · **born in** {seed.get('birthplace')} · **family of** "
            f"{seed.get('family_size')} · **{seed.get('wealth_bracket')}** background",
            f"- **Tier** {TIER_NAMES[a.tier]} → `{model_for(a, settings.llm.models)}` · **role** {a.role} · "
            f"**money** {a.money_cents / 100:.2f} asaps · **bedtime** {a.bedtime_hour:02d}:00",
            f"- **Identity** MAC `{a.virtual_mac}` · IP `{a.virtual_ip}`",
            f"- **Formative events:** {'; '.join(seed.get('formative_events', []))}",
            "",
            f"**Look:** hair {ap.hair}; build {ap.build}; wears {ap.clothing}; never without "
            f"{ap.signature_item}.",
            "",
            f"**Speech:** {p.speech_style}",
            "",
            "| trait | value |", "|---|---|",
            *[f"| {k} | `{_bar(v)}` |" for k, v in p.big_five.model_dump().items()],
            *[f"| {k} | `{_bar(v)}` |" for k, v in p.dark_traits.model_dump().items()],
            f"| moral alignment | **{p.moral_alignment}** |",
            "",
            f"- **Values:** {'; '.join(p.values)}",
            f"- **Fears:** {'; '.join(p.fears)}",
            f"- **Ambitions:** {'; '.join(p.ambitions)}",
            f"- **Secrets:** {'; '.join(p.secrets) or '—'}",
            f"- **Quirks:** {'; '.join(p.quirks) or '—'}",
            "",
            f"**Backstory.** {p.backstory}",
            "",
        ]
    out[2:2] = [f"{len(agents)} citizens. Unpleasant: " + (", ".join(flagged) if flagged else "none"), ""]
    return "\n".join(out)


def _set_flag(settings: Settings, **values: object) -> None:
    sessions = make_session_factory(make_engine(settings.db_url))
    with sessions() as s:
        ws = get_world_state(s)
        for k, v in values.items():
            setattr(ws, k, v)
        s.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asapville")
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init-world", help="create the database, map and first citizens")
    p_init.add_argument("--population", type=int)
    p_run = sub.add_parser("run", help="run the world")
    p_run.add_argument("--ticks", type=int, help="stop after N ticks")
    p_run.add_argument("--days", type=float, help="stop after N simulated days")
    p_run.add_argument("--fast", action="store_true", help="don't wait between ticks")
    sub.add_parser("pause")
    sub.add_parser("resume")
    p_speed = sub.add_parser("speed", help="set tick speed multiplier")
    p_speed.add_argument("value", type=float)
    p_rep = sub.add_parser("report", help="world state, recent events and cost summary")
    p_rep.add_argument("--events", type=int, default=15)
    p_pers = sub.add_parser("personas", help="print every citizen's persona card")
    p_pers.add_argument("--out", help="write markdown persona cards to this file")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)

    if args.cmd == "init-world":
        agents = asyncio.run(init_world(settings, population=args.population))
        print(f"Asapville founded with {len(agents)} citizens: " + ", ".join(a.name for a in agents))
        return 0
    if args.cmd == "pause":
        _set_flag(settings, paused=True)
        print("paused")
        return 0
    if args.cmd == "resume":
        _set_flag(settings, paused=False, mode="normal")
        print("resumed")
        return 0
    if args.cmd == "speed":
        _set_flag(settings, speed=args.value)
        print(f"speed set to {args.value}x")
        return 0

    sessions, gateway = _bootstrap(settings)
    if args.cmd == "report":
        with sessions() as s:
            print(world_report(s, settings, events=args.events))
            print()
            print(cost_report(s))
        return 0
    if args.cmd == "personas":
        with sessions() as s:
            if args.out:
                from pathlib import Path

                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_text(persona_cards_markdown(s, settings))
                print(f"wrote {args.out}")
            else:
                print(persona_cards(s))
        return 0
    if args.cmd == "run":
        ticks = args.ticks
        if args.days is not None:
            ticks = int(args.days * settings.clock.ticks_per_day)
        world = World(settings, sessions, gateway)
        try:
            reports = asyncio.run(world.run(ticks=ticks, realtime=not args.fast))
        except KeyboardInterrupt:
            print("stopped")
            return 0
        for r in reports[-3:]:
            print(f"tick {r.tick} {r.time_label}: {r.llm_calls} calls, {r.events} events, "
                  f"mode {r.mode}{' PAUSED' if r.paused else ''}")
        with sessions() as s:
            print(world_report(s, settings))
            print()
            print(cost_report(s))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
