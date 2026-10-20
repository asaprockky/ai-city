"""The town paper for the Telegram channel: what happened, told so it is worth reading.

`facts()` gathers the state of the town straight from the world (the clock, the fund, the
council and any election, food on the shelves, jobs nobody does, every citizen and what they
think and mean to do, who has died); `happenings()` lists the hours' events with the citizens'
own words. One call to the stronger model turns both into a `Paper`: a headline, stories with
quotes, and a line on everyone. `as_text()` makes the channel post of it and `render_html()` a
self-contained newspaper page with faces, a map, the shelves and the money, sent as a file.

Between papers, `breaking()` picks out the big moments (a death, an election, a business
changing hands, a default, a new law, a secret deal) and `write_flash()` makes a short news
post of each, so the channel hears of them when they happen.
"""

from __future__ import annotations

import base64
import html
import io
import json
import mimetypes
import re
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from asapville.config import Settings
from asapville.db.models import Agent, Event, Location, Memory, WorldState
from asapville.llm.client import LLMGateway
from asapville.llm.routing import ANGEL
from asapville.llm.types import ChatMessage, LLMRequest
from asapville.world import economy, elections
from asapville.world.clock import Clock

SEASONS_RU = {"spring": "весна", "summer": "лето", "autumn": "осень", "winter": "зима"}
QUIET_KINDS = {"move", "rest", "sleep", "idle", "travel", "depart", "arrive", "wake", "plan",
               "invalid_action", "tier_change", "angel", "eat", "work", "ballot"}
BREAKING_KINDS = {"death", "property", "law", "proposal", "candidacy", "bank", "secret_deal",
                  "open_stall", "collapse", "arrival"}
BREAKING_AUTHORS = {"the town clerk", "AsapBank"}  # notices worth a flash: elections, defaults
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

REPORT_RULES = """You are the editor of the Asapville Gazette, the paper of a small town whose \
citizens live their own lives. Readers follow the town like a series: they want to know what \
happened, who wanted what, who said what to whom, who won and who lost, and what may happen next. \
You get the state of the town and the real events of the last hours, with the citizens' own words.

Write in Russian, vividly and in detail, like a good small-town newspaper with a sense of humour: \
concrete names, numbers, places, and short quotes taken from what citizens really said (translate \
them into natural Russian). Explain why things matter: motives, rivalries, debts, deals, hunger. \
Never invent an event, a deal, a death or a quote that is not in the material; if the hours were \
quiet, say so and write about plans and tensions instead.

Reply with ONLY a JSON object:
{"headline": "<the big headline, punchy, at most 80 characters>",
 "lede": "<two or three sentences: the most important thing right now and why it matters>",
 "stories": [
   {"title": "<story title>",
    "body": "<4 to 8 sentences: what happened, in order, with names, numbers, motives>",
    "people": ["<exact citizen names in the story>"],
    "quote": {"who": "<exact citizen name>", "text": "<their words, in Russian>"} or null}
 ],
 "power": "<the council and any election: who stands, what they promise, the votes so far, \
or what the council did>",
 "food_and_work": "<what is on the shelves and at what price, what ran out, which jobs nobody \
does and what they pay, who is getting rich and who is broke>",
 "citizens": [{"name": "<exact name>", "line": "<where they are, what they are doing or plotting, \
one telling detail; one or two sentences>"}],
 "dead": "<names and causes, or empty>",
 "next": "<two or three sentences: what to watch in the coming hours>"}
Write 3 to 5 stories, the strongest first. Every living citizen gets one entry in citizens."""

FLASH_RULES = """You write breaking-news posts for the Asapville town channel. You get one big \
moment that just happened in town and what led up to it. Write a short post in Russian: start \
with "⚡️ " and a punchy headline line, then two to five sentences on what happened, who is \
involved, why it matters and what it may lead to, with a short real quote if there is one. \
Plain text, no markdown. Never invent anything that is not in the material."""


CHUNK_RULES = """You are the town chronicler of Asapville, posting to its Telegram channel after \
every stretch of hours. Readers love short, lively bulletins: what each citizen got up to, with \
their real words. You get the state of the town and the real events of those hours.

Write in Russian, in plain text (no markdown, no asterisks), exactly in this shape:
Часы <first>–<last> (<день N, время с–по>).

• <Bold-sounding lead phrase>. <2–4 sentences: who did what, why, for how much, what came of it.>
   ◦ <a sub-point or a short real quote, if useful>
• ...

<One closing line on food, money or what to watch next.>

Write 6 to 9 bullets, the most dramatic first: deals and price wars, schemes, quarrels, kindness, \
new businesses and dishes, the council and elections, hunger and illness. Name people by their \
first names in Russian and keep numbers exact. Quote citizens' real words (translated into natural \
Russian) in «»; no English may remain in the text, and spell each name the same way every time. \
Never invent anything that is not in the material; if the hours were mostly night, \
say so and keep it shorter."""


class Quote(BaseModel):
    who: str = ""
    text: str = ""


class Story(BaseModel):
    title: str
    body: str
    people: list[str] = Field(default_factory=list)
    quote: Quote | None = None


class CitizenLine(BaseModel):
    name: str
    line: str


class Paper(BaseModel):
    headline: str
    lede: str = ""
    stories: list[Story] = Field(default_factory=list)
    power: str = ""
    food_and_work: str = ""
    citizens: list[CitizenLine] = Field(default_factory=list)
    dead: str = ""
    next: str = ""


def _when(settings: Settings, tick: int) -> tuple[int, str]:
    t = Clock(settings.clock).at(tick)
    return t.day, f"День {t.day}, {t.hour:02d}:00, {SEASONS_RU.get(t.season, t.season)}"


def _plan(session: Session, agent: Agent) -> str | None:
    return session.scalar(select(Memory.content).where(Memory.agent_id == agent.id, Memory.kind == "plan",
                                                       Memory.importance == 5)
                          .order_by(Memory.created_at_tick.desc()).limit(1))


def _shelf(session: Session, settings: Settings, loc: Location) -> list[tuple[str, int, int]]:
    return [(i, economy.stock_qty(session, loc.id, i),
             economy.price_of(settings, i, session, at=loc.key) or 0)
            for i in economy.shop_items(settings, loc.key)]


def facts(session: Session, settings: Settings) -> str:
    ws = session.get(WorldState, 1)
    assert ws is not None
    t = Clock(settings.clock).at(ws.tick)
    locs = {loc.id: loc for loc in session.scalars(select(Location))}
    by_key = {loc.key: loc for loc in locs.values()}
    out = [f"Day {t.day}, {t.hour:02d}:00, {t.season}. Town fund {ws.treasury_cents // 100} asaps."]

    members = elections.council(session, ws.tick)
    if members:
        out.append("Council: " + ", ".join(m.name for m in members))
    e = elections.open_election(session)
    if e is not None:
        votes = elections.tally(session, e)
        cands = "; ".join(f"{session.get(Agent, c.agent_id).name} ({votes.get(c.agent_id, 0)} votes): "
                          f'"{c.pitch[:300]}"' for c in elections.candidates(session, e))
        out.append(f"Council election open, closes in {e.closes_tick - ws.tick} hours. "
                   f"Candidates: {cands or 'none yet'}")

    shelves = []
    for key in ("bakery", "market", "farm"):
        loc = by_key.get(key)
        if loc is not None:
            shelves.append(f"{loc.name}: " + ", ".join(f"{i} {q} at {p / 100:.2f}"
                                                       for i, q, p in _shelf(session, settings, loc)))
    out.append("Shelves: " + " | ".join(shelves))
    empty = [f"{name} ({economy.wage_of(session, settings, name) / 100:.2f}/h)"
             for name in settings.economy.jobs if not economy.workers_in(session, name)]
    out.append("Jobs nobody holds: " + (", ".join(empty) or "none"))
    owners = [f"{loc.name}: {session.get(Agent, loc.owner_id).name}" for loc in locs.values()
              if loc.owner_id and economy.is_business(settings, loc)]
    if owners:
        out.append("Owned businesses: " + "; ".join(owners))

    out.append("Citizens:")
    for a in session.scalars(select(Agent).where(Agent.alive.is_(True)).order_by(Agent.id)):
        where = locs[a.location_id].name if a.location_id in locs else "on the road"
        out.append(f"- {a.name}, {a.role}, at {where}{' (asleep)' if a.asleep else ''}; "
                   f"{a.money_cents / 100:.2f} asaps; fed {100 - a.hunger:.0f}, health {a.health:.0f}, "
                   f"mood {a.mood_label}. Thinking: {(a.last_thought or '')[:400]} "
                   f"Plan today: {(_plan(session, a) or '-')[:400]}")
    dead = list(session.scalars(select(Agent).where(Agent.alive.is_(False))))
    if dead:
        out.append("Dead: " + ", ".join(f"{a.name} ({a.cause_of_death or 'unknown'})" for a in dead))
    return "\n".join(out)


def _line(e: Event) -> str:
    text = e.summary
    said = (e.data or {}).get("line")
    if e.kind == "talk" and said and said not in text:
        text = f'{text.split(":")[0]}: "{said}"'
    return f"[{e.tick}] {e.kind}: {text[:600]}"


def happenings(session: Session, since_tick: int, limit: int = 220) -> str:
    """The hours' events, with what was said in full; meals and shifts are counted, not listed."""
    lines, meals, shifts = [], 0, 0
    for e in session.scalars(select(Event).where(Event.tick >= since_tick).order_by(Event.id)):
        if e.kind == "eat":
            meals += 1
        elif e.kind == "work":
            shifts += 1
        if e.kind not in QUIET_KINDS:
            lines.append(_line(e))
    lines = lines[-limit:]
    lines.append(f"(Also {meals} meals eaten and {shifts} hours of paid work.)")
    return "\n".join(lines)


def parse(text: str) -> Paper:
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    try:
        return Paper.model_validate(json.loads(cleaned[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(str(exc)[:300]) from exc


async def write(session: Session, settings: Settings, gateway: LLMGateway, *, hours: int = 12) -> Paper:
    ws = session.get(WorldState, 1)
    tick = ws.tick if ws else 0
    prompt = (f"THE TOWN NOW:\n{facts(session, settings)}\n\nWHAT HAPPENED IN THE LAST {hours} HOURS "
              f"([hour] kind: what):\n{happenings(session, max(0, tick - hours))}\n\n"
              "Write the paper. Reply with only the JSON.")
    req = LLMRequest(purpose="digest", model=settings.llm.models.sonnet, tier=ANGEL, effort=None,
                     system_rules=REPORT_RULES, angel_role="reporter", max_tokens=9000,
                     messages=[ChatMessage(role="user", content=prompt)], context={"tick": tick})
    result = await gateway.call(req, tick=tick)
    return parse(result.text)


def as_text(session: Session, settings: Settings, paper: Paper) -> str:
    """The channel post: the whole paper as plain text."""
    ws = session.get(WorldState, 1)
    assert ws is not None
    _, when = _when(settings, ws.tick)
    out = [f"🗞 {paper.headline}", f"{when}. В фонде города {ws.treasury_cents // 100} асапов.", "",
           paper.lede]
    for st in paper.stories:
        out += ["", f"▪️ {st.title}", st.body]
        if st.quote and st.quote.text:
            out.append(f"— «{st.quote.text}» ({st.quote.who})")
    if paper.power:
        out += ["", "🏛 Власть", paper.power]
    if paper.food_and_work:
        out += ["", "🍞 Еда и работа", paper.food_and_work]
    if paper.citizens:
        out += ["", "👥 Где кто"] + [f"• {c.name}: {c.line}" for c in paper.citizens]
    if paper.dead:
        out += ["", f"⚰️ Умерли: {paper.dead}"]
    if paper.next:
        out += ["", f"👀 Что дальше: {paper.next}"]
    return "\n".join(out)


async def write_chunk(session: Session, settings: Settings, gateway: LLMGateway, *,
                      hours: int = 12, note: str = "") -> str:
    """The bulletin for the last `hours`: bullets with quotes, the way the channel likes it."""
    ws = session.get(WorldState, 1)
    tick = ws.tick if ws else 0
    first = max(0, tick - hours)
    _, start = _when(settings, first)
    _, end = _when(settings, tick)
    prompt = (f"HOURS {first}–{tick - 1} ({start} to {end}).\n\nTHE TOWN NOW:\n{facts(session, settings)}"
              f"\n\nWHAT HAPPENED ([hour] kind: what):\n{happenings(session, first)}\n\nWrite the bulletin.")
    req = LLMRequest(purpose="chunk", model=settings.llm.models.sonnet, tier=ANGEL, effort=None,
                     system_rules=CHUNK_RULES, angel_role="reporter", max_tokens=4000,
                     messages=[ChatMessage(role="user", content=prompt)], context={"tick": tick})
    result = await gateway.call(req, tick=tick)
    text = result.text.strip().replace("**", "")
    return f"{text}\n\n🛠 {note}" if note else text


# ------------------------------------------------------------------ breaking news

def breaking(session: Session, after_event_id: int) -> list[Event]:
    """The big moments since `after_event_id`, oldest first."""
    return list(session.scalars(select(Event).where(Event.id > after_event_id, or_(
        Event.kind.in_(BREAKING_KINDS), Event.kind == "notice")).order_by(Event.id)))


def is_breaking(e: Event) -> bool:
    return e.kind in BREAKING_KINDS or (e.data or {}).get("author") in BREAKING_AUTHORS


async def write_flash(session: Session, settings: Settings, gateway: LLMGateway, e: Event) -> str:
    """A short news post about one big moment, with what led up to it."""
    people = [i for i in (e.agent_id, e.target_id) if i is not None]
    lead_up = session.scalars(select(Event).where(
        Event.tick >= e.tick - 12, Event.id < e.id, Event.kind.not_in(QUIET_KINDS),
        or_(Event.agent_id.in_(people), Event.target_id.in_(people)) if people else Event.id < 0)
        .order_by(Event.id.desc()).limit(25))
    _, when = _when(settings, e.tick)
    prompt = (f"{when}.\nTHE MOMENT: {_line(e)}\n\nWHAT LED UP TO IT:\n"
              + "\n".join(_line(x) for x in reversed(list(lead_up)))
              + f"\n\nTHE TOWN NOW:\n{facts(session, settings)}\n\nWrite the post.")
    req = LLMRequest(purpose="flash", model=settings.llm.models.sonnet, tier=ANGEL, effort=None,
                     system_rules=FLASH_RULES, angel_role="reporter", max_tokens=1500,
                     messages=[ChatMessage(role="user", content=prompt)], context={"event": e.id})
    result = await gateway.call(req, tick=e.tick)
    return result.text.strip()


# ------------------------------------------------------------------ the paper as a page

def _face(agent: Agent, size: int = 160) -> str:
    """A small embedded portrait (manga/faces), or the citizen's ink bust."""
    from asapville import comic

    f = comic.face_file(agent)
    if f is None:
        return comic.face_svg(agent, 72)
    from PIL import Image

    im = Image.open(f).convert("RGB")
    im.thumbnail((size, size))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    data = base64.b64encode(buf.getvalue()).decode()
    return f'<img class="face" alt="" src="data:image/jpeg;base64,{data}">'


def _map(session: Session, people: list[Agent]) -> str:
    locs = list(session.scalars(select(Location)))
    if not locs:
        return ""
    xs, ys = [loc.x for loc in locs], [loc.y for loc in locs]
    x0, y0 = min(xs), min(ys)
    w, h = max(max(xs) - x0, 1), max(max(ys) - y0, 1)
    here: dict[int, list[str]] = {}
    for a in people:
        if a.location_id is not None:
            here.setdefault(a.location_id, []).append(a.name.split()[0])
    out = ['<svg viewBox="0 0 640 400" class="map" role="img" aria-label="Карта города">']
    for loc in locs:
        x, y = 60 + (loc.x - x0) / w * 520, 40 + (loc.y - y0) / h * 320
        who = here.get(loc.id, [])
        r = 5 + 3 * len(who)
        out.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{r}" class="{"busy" if who else "dot"}"/>')
        if who or loc.kind != "home":  # the houses crowd each other; name one only when someone is in
            out.append(f'<text x="{x:.0f}" y="{y - r - 4:.0f}" class="place">{html.escape(loc.name)}</text>')
        if who:
            out.append(f'<text x="{x:.0f}" y="{y + r + 12:.0f}" class="who">'
                       f'{html.escape(", ".join(who))}</text>')
    out.append("</svg>")
    return "".join(out)


PAGE_STYLE = """
:root{--bg:#f6f1e7;--ink:#1d1a16;--soft:#6b6257;--rule:#cfc4b2;--card:#fffdf8;--accent:#a3301f;
--fed:#6b8f3a;--hp:#2f6f9f}
@media (prefers-color-scheme:dark){:root{--bg:#16130f;--ink:#efe8dc;--soft:#a89e90;--rule:#3a342c;
--card:#1f1b16;--accent:#e0644f;--fed:#8fb85a;--hp:#5aa0d6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:17px/1.55 Georgia,'Times New Roman',serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}
header{text-align:center;border-bottom:4px double var(--ink);padding-bottom:12px}
.mast{font:700 clamp(30px,7vw,60px)/1 Georgia,serif;letter-spacing:.02em}
.date{color:var(--soft);font-size:14px;text-transform:uppercase;letter-spacing:.12em;margin-top:6px}
h1{font-size:clamp(26px,5vw,42px);line-height:1.15;margin:22px 0 8px}
.lede{font-size:20px;border-bottom:1px solid var(--rule);padding-bottom:16px}
.cols{columns:2 340px;column-gap:32px;margin-top:18px}
article{break-inside:avoid;margin:0 0 22px;padding-bottom:16px;border-bottom:1px solid var(--rule)}
article h2{font-size:22px;margin:0 0 6px;line-height:1.2}
.cast{display:flex;gap:6px;margin:6px 0}.cast .face,.cast svg{width:48px;height:48px;
border-radius:50%;object-fit:cover}
blockquote{margin:10px 0 0;padding:6px 0 6px 14px;border-left:3px solid var(--accent);font-style:italic}
cite{display:block;font-style:normal;color:var(--soft);font-size:14px;margin-top:2px}
.box{background:var(--card);border:1px solid var(--rule);padding:14px 16px;margin:16px 0;
border-radius:4px}
.box p{margin:0}
.box h3,h2.sec{font:700 13px/1 system-ui,sans-serif;text-transform:uppercase;letter-spacing:.14em;
color:var(--accent);margin:0 0 8px}
h2.sec{margin:32px 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}
.card{display:flex;gap:12px;background:var(--card);border:1px solid var(--rule);border-radius:6px;
padding:12px}
.card .face,.card svg{width:72px;height:72px;border-radius:6px;object-fit:cover;flex:none}
.info{min-width:0;flex:1}.info p{margin:6px 0;font-size:15px}
.role{display:block;color:var(--soft);font:13px system-ui,sans-serif}
.bar{position:relative;height:16px;background:var(--rule);border-radius:3px;margin-top:4px;
overflow:hidden;font:11px/16px system-ui,sans-serif}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--accent);opacity:.75}
.bar.fed i{background:var(--fed)}.bar.hp i{background:var(--hp)}
.bar span{position:relative;padding-left:6px}
table{width:100%;border-collapse:collapse;font:15px system-ui,sans-serif}td{padding:6px 8px;
border-bottom:1px solid var(--rule)}
tr.out td{color:var(--accent)}
.map{width:100%;height:auto;background:var(--card);border:1px solid var(--rule);border-radius:6px}
.map .dot{fill:var(--rule)}.map .busy{fill:var(--accent);opacity:.8}
.map text{text-anchor:middle;font:11px system-ui,sans-serif;fill:var(--soft)}
.map .who{fill:var(--ink);font-weight:600}
footer{color:var(--soft);font-size:13px;text-align:center;margin-top:40px}
"""


def render_html(session: Session, settings: Settings, paper: Paper) -> str:
    """The paper as one self-contained page: stories, faces, a map, shelves and money."""
    ws = session.get(WorldState, 1)
    assert ws is not None
    day, when = _when(settings, ws.tick)
    esc = html.escape
    living = list(session.scalars(select(Agent).where(Agent.alive.is_(True)).order_by(Agent.id)))
    by_name = {a.name: a for a in living}
    locs = {loc.id: loc for loc in session.scalars(select(Location))}

    stories = []
    for st in paper.stories:
        faces = "".join(_face(by_name[n], 96) for n in st.people if n in by_name)
        quote = (f"<blockquote>«{esc(st.quote.text)}»<cite>{esc(st.quote.who)}</cite></blockquote>"
                 if st.quote and st.quote.text else "")
        stories.append(f'<article><h2>{esc(st.title)}</h2><div class="cast">{faces}</div>'
                       f"<p>{esc(st.body)}</p>{quote}</article>")

    lines = {c.name: c.line for c in paper.citizens}
    richest = max((a.money_cents for a in living), default=1) or 1
    cards = []
    for a in sorted(living, key=lambda a: -a.money_cents):
        where = locs[a.location_id].name if a.location_id in locs else "в пути"
        fed = max(0.0, 100 - a.hunger)
        cards.append(
            f'<div class="card">{_face(a)}<div class="info"><b>{esc(a.name)}</b>'
            f'<span class="role">{esc(a.role)} · {esc(where)}{" · спит" if a.asleep else ""}</span>'
            f"<p>{esc(lines.get(a.name, ''))}</p>"
            f'<div class="bar"><i style="width:{a.money_cents / richest * 100:.0f}%"></i>'
            f"<span>{a.money_cents / 100:.2f} асапов</span></div>"
            f'<div class="bar fed"><i style="width:{fed:.0f}%"></i><span>сытость {fed:.0f}</span></div>'
            f'<div class="bar hp"><i style="width:{max(0.0, a.health):.0f}%"></i>'
            f"<span>здоровье {a.health:.0f}</span></div></div></div>")

    rows = []
    for key in ("bakery", "market", "farm"):
        loc = next((v for v in locs.values() if v.key == key), None)
        if loc is not None:
            rows += [f'<tr class="{"out" if q == 0 else ""}"><td>{esc(loc.name)}</td><td>{esc(i)}</td>'
                     f"<td>{q}</td><td>{p / 100:.2f}</td></tr>" for i, q, p in _shelf(session, settings, loc)]

    def box(title: str, text: str) -> str:
        return f'<section class="box"><h3>{title}</h3><p>{esc(text)}</p></section>' if text else ""

    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Асапвильская газета, день {day}</title><style>{PAGE_STYLE}</style></head><body><main>"
            f'<header><div class="mast">Асапвильская газета</div><div class="date">{esc(when)} · '
            f"в фонде города {ws.treasury_cents // 100} асапов</div></header>"
            f'<h1>{esc(paper.headline)}</h1><p class="lede">{esc(paper.lede)}</p>'
            f'<div class="cols">{"".join(stories)}</div>'
            f'{box("Власть", paper.power)}{box("Еда и работа", paper.food_and_work)}'
            f'{box("Что дальше", paper.next)}'
            f'<h2 class="sec">Где кто сейчас</h2>{_map(session, living)}'
            f'<h2 class="sec">Жители</h2><div class="grid">{"".join(cards)}</div>'
            f'<h2 class="sec">Полки</h2><table>{"".join(rows)}</table>'
            f'{box("Умерли", paper.dead)}'
            "<footer>Всё в этой газете случилось в Асапвиле на самом деле: жители решают сами.</footer>"
            "</main></body></html>")


def post_document(path: Path, caption: str, *, token: str, chat: str) -> dict[str, Any]:
    """Send a file (an HTML page, say) to the Telegram channel; readers open it from the chat."""
    boundary = uuid.uuid4().hex
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in (("chat_id", chat), ("caption", caption[:1024]))]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"'
                 f"\r\nContent-Type: {kind}\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())
