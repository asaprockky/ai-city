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
from asapville.world.economy import open_economy
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
    guard = BudgetGuard(settings.budget, opus_model=settings.llm.models.opus,
                        cheap_model=settings.llm.models.haiku)
    gateway = LLMGateway(make_backend(settings.llm), settings.llm, guard)
    return sessions, gateway


async def init_world(settings: Settings, *, population: int | None = None,
                     sessions: sessionmaker[Session] | None = None,
                     gateway: LLMGateway | None = None) -> list[Agent]:
    """Migrate the DB, create map, founding brands and the first citizens."""
    if sessions is None or gateway is None:
        # Build the LLM backend first: a missing API key fails before any DB file is touched.
        sessions, gateway = _bootstrap(settings)
    upgrade_to_head(settings.db_url)
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
        open_economy(s, settings, tick=0)
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
             f"{'PAUSED' if ws.paused else 'running'} | weather {ws.weather} | "
             f"fund {ws.treasury_cents / 100:.2f}", "",
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


async def serve(settings: Settings, sessions: sessionmaker[Session], gateway: LLMGateway, *,
                host: str, port: int, ticks: int | None, realtime: bool, run_world: bool) -> None:
    """Serve the dashboard; by default also run the world in this process so updates are instant."""
    import uvicorn

    from asapville.dashboard.app import create_app
    from asapville.world.events import EventBus

    bus = EventBus()
    world = World(settings, sessions, gateway, bus) if run_world else None
    app = create_app(settings, sessions, bus if world else None)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    print(f"Asapville dashboard: http://{host}:{port}  (Ctrl-C to stop)")
    server_task = asyncio.create_task(server.serve())
    world_task = asyncio.create_task(world.run(ticks=ticks, realtime=realtime)) if world else None

    async def announce_end() -> None:
        if world_task is None:
            return
        try:
            await world_task
            print("world run finished; the dashboard stays up until Ctrl-C")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("world run failed")

    end_task = asyncio.create_task(announce_end())
    try:
        await server_task
    finally:
        if world_task is not None and not world_task.done():
            world_task.cancel()
        await asyncio.gather(end_task, return_exceptions=True)


async def arrive(settings: Settings, sessions: sessionmaker[Session], gateway: LLMGateway, *,
                 preset: str | None = None, works_for: str | None = None,
                 advance_cents: int = 4000, lead: bool = False) -> Agent:
    """A newcomer is born (seed card rolled, or a preset), gets a free house if any, and arrives."""
    from asapville.agents.birth import roll_seed_card
    from asapville.agents.presets import PRESETS
    from asapville.world.events import emit, remember

    if preset is not None and preset not in PRESETS:
        raise SystemExit(f"unknown preset {preset!r}; known: {', '.join(PRESETS)}")
    with sessions() as s:
        gateway.guard.load(s)
        ws = get_world_state(s)
        n = s.scalar(select(func.count(Agent.id))) or 0
        rng = random.Random(f"{settings.world.seed}:arrive:{ws.tick}:{n}")
        seed_locations(s, settings.map)  # new house plots, if the town has grown
        seed = PRESETS[preset].model_copy() if preset else roll_seed_card(rng, settings)
        homes = s.scalars(select(Location).where(Location.kind == "home").order_by(Location.id))
        home = next((loc for loc in homes if loc.owner_id is None), None)
        agent = await birth_agent(s, gateway, settings, rng, seed_index=n, home=home, tick=ws.tick,
                                  seed=seed)
        if home is not None:
            home.owner_id = agent.id
        agent.lead = lead
        emit(s, tick=ws.tick, kind="arrival", actor=agent, location_id=agent.location_id,
             visibility="public",
             summary=f"A newcomer moved to {settings.world.name}: {agent.name}",
             data={"preset": preset, "home": home.name if home else None, "lead": lead},
             actor_memory=f"Today I moved to {settings.world.name}"
                          + (f" and took {home.name} as my home." if home
                             else ", with no home of my own yet."),
             importance=6)
        if works_for:
            _secret_deal(s, ws.tick, agent, works_for, advance_cents)
        for other in s.scalars(select(Agent).where(Agent.alive.is_(True), Agent.id != agent.id)):
            remember(s, other.id, ws.tick, "observation",
                     "Word is going round that a newcomer has moved to town.", importance=3)
        gateway.flush(s)
        s.commit()
        return agent


def _secret_deal(s: Session, tick: int, agent: Agent, patron_name: str, advance_cents: int) -> None:
    """The newcomer was paid by a citizen to watch the town and report back. Only the two know."""
    from asapville.world.events import emit, remember
    from asapville.world.social import introduce

    wanted = patron_name.casefold()
    patron = next((a for a in s.scalars(select(Agent).where(Agent.alive.is_(True), Agent.id != agent.id))
                   if a.name.casefold() == wanted or a.name.split()[0].casefold() == wanted), None)
    if patron is None:
        raise SystemExit(f"no living citizen called {patron_name!r}")
    paid = min(advance_cents, patron.money_cents)
    patron.money_cents -= paid
    agent.money_cents += paid
    introduce(s, agent, patron, tick)
    remember(s, agent.id, tick, "plan",
             f"{patron.name} paid my way to this town and {paid / 100:.2f} asaps up front. In return I am to "
             f"befriend ordinary people, learn what they say and plan, and report it to {patron.name}, who "
             "pays for anything useful. Nobody else may know.", importance=9)
    remember(s, patron.id, tick, "plan",
             f"I paid {agent.name} {paid / 100:.2f} asaps to come to town, mix with ordinary people and "
             "bring "
             "me what they say and plan. More money for anything useful. Nobody else may know.", importance=9)
    emit(s, tick=tick, kind="secret_deal", actor=patron, target=agent, visibility="private",
         summary=f"Secretly, {patron.name} paid {agent.name} {paid / 100:.2f} asaps to spy on the town",
         data={"cents": paid, "patron_id": patron.id})


def _live_writer(settings: Settings, sessions: sessionmaker[Session], live_dir: str):
    """after_step hook: write `t<tick>.json` (atomically) for whoever relays the live feed."""
    import json
    from pathlib import Path

    from asapville.dashboard.snapshot import tick_bundle

    out = Path(live_dir)
    out.mkdir(parents=True, exist_ok=True)

    async def write(report) -> None:
        with sessions() as s:
            bundle = tick_bundle(s, settings, report.tick)
        tmp = out / f".t{report.tick:06d}.json"
        tmp.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.rename(out / f"t{report.tick:06d}.json")

    return write


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
    p_run.add_argument("--live-dir", help="write one JSON bundle per finished tick here (live feeds)")
    sub.add_parser("pause")
    sub.add_parser("resume")
    p_speed = sub.add_parser("speed", help="set tick speed multiplier")
    p_speed.add_argument("value", type=float)
    p_rep = sub.add_parser("report", help="world state, recent events and cost summary")
    p_rep.add_argument("--events", type=int, default=15)
    p_pers = sub.add_parser("personas", help="print every citizen's persona card")
    p_pers.add_argument("--out", help="write markdown persona cards to this file")
    p_serve = sub.add_parser("serve", help="run the world with a live dashboard in the browser")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8420)
    p_serve.add_argument("--ticks", type=int, help="stop the world after N ticks (keeps serving)")
    p_serve.add_argument("--days", type=float, help="stop the world after N simulated days")
    p_serve.add_argument("--fast", action="store_true", help="don't wait between ticks")
    p_serve.add_argument("--view-only", action="store_true",
                         help="don't run the world here; just show the database (live if `run` is on)")
    p_arr = sub.add_parser("arrive", help="a newcomer moves to town (their own model writes who they are)")
    p_arr.add_argument("--preset", help="a seed card from asapville/agents/presets.py, e.g. demagogue")
    p_arr.add_argument("--works-for", help="a citizen (name or first name) who secretly paid their way here")
    p_arr.add_argument("--advance", type=float, default=40.0, help="asaps the patron already paid them")
    p_arr.add_argument("--count", type=int, default=1, help="how many newcomers arrive")
    p_arr.add_argument("--lead", action="store_true", help="make them main heroes (always the strong model)")
    p_exp = sub.add_parser("export", help="write a self-contained replay page of the town")
    p_exp.add_argument("--out", default="asapville.html")
    p_por = sub.add_parser("portraits", help="draw manga portraits (Gemini) for citizens who have none")
    p_por.add_argument("--redo", type=int, help="redraw this citizen's portrait (their id)")
    p_com = sub.add_parser("comic", help="draw the last hours as a manga page (and post it to Telegram)")
    p_com.add_argument("--hours", type=int, default=24, help="how many past hours the chapter covers")
    p_com.add_argument("--out", default="asapville-manga.png")
    p_com.add_argument("--script", help="redraw from a saved script JSON instead of writing a new one")
    p_com.add_argument("--post", action="store_true",
                       help="send it to Telegram (ASAPVILLE_TELEGRAM_TOKEN, ASAPVILLE_TELEGRAM_CHAT)")
    p_dig = sub.add_parser("digest", help="write the town paper in Russian (and post it to Telegram)")
    p_dig.add_argument("--hours", type=int, default=12, help="how many past hours the paper covers")
    p_dig.add_argument("--out", default="asapville-gazette", help="where to save the .json and .html")
    p_dig.add_argument("--post", action="store_true",
                       help="send it to Telegram (ASAPVILLE_TELEGRAM_TOKEN, ASAPVILLE_TELEGRAM_CHAT)")
    p_chk = sub.add_parser("chunk", help="a bulletin of the last hours, bullets with quotes (and post it)")
    p_chk.add_argument("--hours", type=int, default=12)
    p_chk.add_argument("--note", default="", help="a line from the builder: what was fixed or changed")
    p_chk.add_argument("--post", action="store_true")
    p_cast = sub.add_parser("cast", help="the who-is-who page of every citizen (post and pin it)")
    p_cast.add_argument("--out", default="kto-est-kto.html")
    p_cast.add_argument("--redo", action="store_true", help="rewrite every citizen's profile")
    p_cast.add_argument("--post", action="store_true", help="send it to the channel and pin it")
    p_news = sub.add_parser("news", help="breaking-news posts for the big moments since the last run")
    p_news.add_argument("--max", type=int, default=4, help="at most this many posts per run")
    p_news.add_argument("--mark", default=".asapville-news", help="file remembering the last event posted")
    p_news.add_argument("--post", action="store_true")
    p_doc = sub.add_parser("post-file", help="send a file (an HTML page, say) to the Telegram channel")
    p_doc.add_argument("path")
    p_doc.add_argument("--caption", default="")
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
    if args.cmd == "arrive":
        upgrade_to_head(settings.db_url)
        for _ in range(args.count):
            agent = asyncio.run(arrive(settings, sessions, gateway, preset=args.preset,
                                       works_for=args.works_for, advance_cents=round(args.advance * 100),
                                       lead=args.lead))
            print(f"{agent.name} ({agent.role}) arrived in {settings.world.name}")
        return 0
    if args.cmd == "portraits":
        import os

        from asapville import comic, images

        key = (os.environ.get("ASAPVILLE_GEMINI_KEY") or os.environ.get("GEMINI_API"))
        if images.available() is not None:
            key = None  # a working image service beats an unpaid Gemini key
        elif not key:
            raise SystemExit("set CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN, FAL_KEY or OPENAI_API_KEY")
        with sessions() as s:
            if args.redo:
                print(comic.draw_portrait(s.get(Agent, args.redo), key=key))
            else:
                print("drew:", ", ".join(comic.draw_missing_portraits(s, key=key)) or "nobody (all done)")
        return 0
    if args.cmd == "comic":
        import os
        from pathlib import Path

        from asapville import comic, images

        if images.available() is not None:
            with sessions() as s:
                try:
                    comic.draw_missing_portraits(s)  # newcomers get a face before their debut
                except Exception as exc:  # a missing portrait only means an ink bust this chapter
                    print(f"portraits: {exc}")
        with sessions() as s:
            tick = get_world_state(s).tick
            if args.script:
                script = comic.Script.model_validate_json(Path(args.script).read_text(encoding="utf-8"))
            else:
                script = asyncio.run(comic.write_script(s, gateway, settings.llm.models.sonnet,
                                                        max(0, tick - args.hours), tick))
                Path(args.out).with_suffix(".json").write_text(script.model_dump_json(indent=1),
                                                                encoding="utf-8")
            day = Clock(settings.clock).at(tick).day
            art = comic.draw_panels(s, script, Path(args.out).with_suffix(""))
            page = comic.render_html(s, script, chapter=f"· ДЕНЬ {day}", art=art)
        out = comic.render_png(page, Path(args.out))
        print(f"wrote {out}: {script.title}")
        if args.post:
            token = os.environ.get("ASAPVILLE_TELEGRAM_TOKEN")
            chat = os.environ.get("ASAPVILLE_TELEGRAM_CHAT")
            if not token or not chat:
                raise SystemExit("set ASAPVILLE_TELEGRAM_TOKEN and ASAPVILLE_TELEGRAM_CHAT to post")
            res = comic.post_telegram(out, f"Асапвиль, день {day}: {script.title}", token=token, chat=chat)
            print("posted" if res.get("ok") else f"telegram said: {res}")
        return 0
    if args.cmd == "cast":
        import os
        from pathlib import Path

        from asapville import cast, digest

        with sessions() as s:
            gateway.guard.load(s)
            profiles = asyncio.run(cast.write_profiles(s, settings, gateway, redo=args.redo))
            gateway.flush(s)
            s.commit()
            Path(args.out).write_text(cast.render_html(s, settings, profiles), encoding="utf-8")
        print(f"wrote {args.out}")
        if args.post:
            token = os.environ.get("ASAPVILLE_TELEGRAM_TOKEN", "")
            chat = os.environ.get("ASAPVILLE_TELEGRAM_CHAT", "")
            res = digest.post_document(Path(args.out), "📌 Кто есть кто в Асапвиле: все жители, их работа, "
                                       "деньги, тайны и симпатии. Открой в браузере.", token=token, chat=chat)
            if res.get("ok"):
                res = cast.pin(res["result"]["message_id"], token=token, chat=chat)
            print("posted and pinned" if res.get("ok") else f"telegram said: {res}")
        return 0
    if args.cmd in ("digest", "news", "chunk", "post-file"):
        import os
        from pathlib import Path

        from asapville import comic, digest

        token = os.environ.get("ASAPVILLE_TELEGRAM_TOKEN", "")
        chat = os.environ.get("ASAPVILLE_TELEGRAM_CHAT", "")
        if (args.cmd == "post-file" or args.post) and (not token or not chat):
            raise SystemExit("set ASAPVILLE_TELEGRAM_TOKEN and ASAPVILLE_TELEGRAM_CHAT to post")
        if args.cmd == "post-file":
            res = digest.post_document(Path(args.path), args.caption, token=token, chat=chat)
            print("posted" if res.get("ok") else f"telegram said: {res}")
            return 0
        with sessions() as s:
            gateway.guard.load(s)
            if args.cmd == "chunk":
                text = asyncio.run(digest.write_chunk(s, settings, gateway, hours=args.hours, note=args.note))
                print(text)
                if len(text) < 80:
                    raise SystemExit("the bulletin came back empty; not posting")
                if args.post:
                    res = comic.post_text(text, token=token, chat=chat)
                    print("posted" if res.get("ok") else f"telegram said: {res}")
            elif args.cmd == "news":
                mark = Path(args.mark)
                tick = get_world_state(s).tick
                after = int(mark.read_text()) if mark.exists() else (s.scalar(
                    select(func.min(Event.id)).where(Event.tick >= tick - 12)) or 1) - 1
                big = [e for e in digest.breaking(s, after) if digest.is_breaking(e)]
                if big:
                    after = max(e.id for e in digest.breaking(s, after))
                for e in big[-args.max:]:
                    text = asyncio.run(digest.write_flash(s, settings, gateway, e))
                    print(text, "\n")
                    if args.post and len(text) > 40:
                        comic.post_text(text, token=token, chat=chat)
                if args.post or not mark.exists():
                    mark.write_text(str(after))
                print(f"{len(big)} big moments")
            else:
                paper = asyncio.run(digest.write(s, settings, gateway, hours=args.hours))
                text = digest.as_text(s, settings, paper)
                page = digest.render_html(s, settings, paper)
                day = Clock(settings.clock).at(get_world_state(s).tick).day
            gateway.flush(s)
            s.commit()
        if args.cmd in ("news", "chunk"):
            return 0
        out = Path(args.out)
        out.with_suffix(".json").write_text(paper.model_dump_json(indent=1), encoding="utf-8")
        page_file = out.with_name(f"gazeta-den-{day}.html")
        page_file.write_text(page, encoding="utf-8")
        print(text)
        print(f"wrote {page_file}")
        if args.post:
            res = comic.post_text(text, token=token, chat=chat)
            if res.get("ok"):
                caption = f"🗞 {paper.headline} — газета целиком, с лицами и картой"
                res = digest.post_document(page_file, caption, token=token, chat=chat)
            print("posted" if res.get("ok") else f"telegram said: {res}")
        return 0
    if args.cmd == "export":
        from pathlib import Path

        from asapville.dashboard.export import export_html

        with sessions() as s:
            Path(args.out).write_text(export_html(s, settings), encoding="utf-8")
        print(f"wrote {args.out}")
        return 0
    if args.cmd == "serve":
        ticks = args.ticks
        if args.days is not None:
            ticks = int(args.days * settings.clock.ticks_per_day)
        upgrade_to_head(settings.db_url)
        try:
            asyncio.run(serve(settings, sessions, gateway, host=args.host, port=args.port,
                              ticks=ticks, realtime=not args.fast, run_world=not args.view_only))
        except KeyboardInterrupt:
            print("stopped")
        return 0
    if args.cmd == "run":
        ticks = args.ticks
        if args.days is not None:
            ticks = int(args.days * settings.clock.ticks_per_day)
        upgrade_to_head(settings.db_url)
        world = World(settings, sessions, gateway)
        after_step = _live_writer(settings, sessions, args.live_dir) if args.live_dir else None
        try:
            reports = asyncio.run(world.run(ticks=ticks, realtime=not args.fast, after_step=after_step))
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
