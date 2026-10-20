"""The town report for the Telegram channel: facts from the database, written up by Claude.

`facts()` gathers what a reader needs, straight from the world: the clock, the town fund, the
council and any election, food on the shelves, jobs nobody does, where each citizen is and
what they mean to do, and who has died. One call to the stronger model turns it into a short
report in Russian, always in the same shape.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.config import Settings
from asapville.db.models import Agent, Location, Memory, WorldState
from asapville.llm.client import LLMGateway
from asapville.llm.routing import ANGEL
from asapville.llm.types import ChatMessage, LLMRequest
from asapville.world import economy, elections
from asapville.world.clock import Clock

REPORT_RULES = """You are the town reporter of Asapville, writing for its Telegram channel. From the \
facts you get, write a short report in Russian, exactly in this shape:

День <N>, <ЧЧ:ММ>, <сезон по-русски>. В фонде города <сумма> асапов.

<Главное сейчас: one paragraph about the most important thing going on (an election, a scheme, \
a crisis), with names and what they promise or want.>

Еда и работа: <one or two sentences: what is on the shelves, what has run out, which jobs nobody \
does and what they pay.>

Где кто:
- <Name> <where they are and what they are doing or planning, with one telling detail>
- ... (every living citizen, one line each)

Умерли: <names and causes, or omit this line if nobody has died>

Plain text, no markdown, no emoji except none. Use the citizens' names as given. Keep numbers \
exact. Do not invent anything that is not in the facts."""


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
                          f'"{c.pitch[:200]}"' for c in elections.candidates(session, e))
        out.append(f"Council election open, closes in {e.closes_tick - ws.tick} hours. "
                   f"Candidates: {cands or 'none yet'}")

    shelves = []
    for key in ("bakery", "market", "farm"):
        loc = by_key.get(key)
        if loc is None:
            continue
        stock = {i: economy.stock_qty(session, loc.id, i) for i in economy.shop_items(settings, key)}
        shelves.append(f"{loc.name}: " + ", ".join(f"{i} {q}" for i, q in stock.items()))
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
        plan = session.scalar(select(Memory.content).where(Memory.agent_id == a.id, Memory.kind == "plan",
                                                           Memory.importance == 5)
                              .order_by(Memory.created_at_tick.desc()).limit(1))
        out.append(f"- {a.name}, {a.role}, at {where}{' (asleep)' if a.asleep else ''}; "
                   f"{a.money_cents / 100:.2f} asaps; fed {100 - a.hunger:.0f}, health {a.health:.0f}. "
                   f"Thinking: {(a.last_thought or '')[:220]} Plan today: {(plan or '-')[:220]}")
    dead = list(session.scalars(select(Agent).where(Agent.alive.is_(False))))
    if dead:
        out.append("Dead: " + ", ".join(f"{a.name} ({a.cause_of_death or 'unknown'})" for a in dead))
    return "\n".join(out)


async def write(session: Session, settings: Settings, gateway: LLMGateway) -> str:
    ws = session.get(WorldState, 1)
    req = LLMRequest(purpose="digest", model=settings.llm.models.sonnet, tier=ANGEL, effort=None,
                     system_rules=REPORT_RULES, angel_role="reporter", max_tokens=5000,
                     messages=[ChatMessage(role="user", content=facts(session, settings)
                                           + "\n\nWrite the report.")],
                     context={"tick": ws.tick if ws else 0})
    result = await gateway.call(req, tick=ws.tick if ws else 0)
    return result.text.strip()


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
