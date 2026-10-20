"""Who is who in Asapville: one page to look up every citizen, pinned in the channel.

Each citizen's portrait in Russian (who they are in a line, character, what they want, their
secret, how they talk) is written once by the stronger model from their own personality card
and kept in `saves/cast.json`; a newcomer gets theirs on the next run. Everything that changes
(job, home, money, what they own, the council, the bank, whom they like and distrust) is read
from the world each time the page is made.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.config import Settings
from asapville.db.models import Agent, BankAccount, Location, Relationship, WorldState
from asapville.digest import PAGE_STYLE, _face, _when
from asapville.llm.client import LLMGateway
from asapville.llm.routing import ANGEL
from asapville.llm.types import ChatMessage, LLMRequest
from asapville.world import economy, elections

PROFILES = Path(__file__).resolve().parent.parent / "saves" / "cast.json"
ROLES_RU = {"baker": "пекарь", "farmer": "фермер", "miller": "мельник", "herder": "пастух",
            "fisher": "рыбак", "quarryman": "каменотёс", "unemployed": "без работы"}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

PROFILE_RULES = """You write the "who is who" page of Asapville for readers who follow the town \
in Russian and keep forgetting who is who. For each citizen you get their personality card. \
Write, in Russian, short and vivid:
- ru_name: their full name in Russian letters;
- who: who they are in one line (e.g. "пекарь-интриган, владеет половиной города");
- character: two or three sentences on their nature and manner;
- wants: what they are after, one or two sentences;
- secret: their secret, one sentence (the other citizens do not know it);
- voice: how they talk, one short line;
- remember_by: one memorable detail to recognise them by (a habit, an item, a look).
Use only what is in the card. Reply with ONLY a JSON object:
{"profiles": [{"name": "<exact name as given>", "ru_name": "", "who": "", "character": "", \
"wants": "", "secret": "", "voice": "", "remember_by": ""}]}"""


class Profile(BaseModel):
    name: str
    ru_name: str = ""
    who: str = ""
    character: str = ""
    wants: str = ""
    secret: str = ""
    voice: str = ""
    remember_by: str = ""


class Profiles(BaseModel):
    profiles: list[Profile] = Field(default_factory=list)


def _card(agent: Agent) -> str:
    p, seed = agent.personality or {}, agent.seed_card or {}
    look = p.get("appearance") or {}
    return (f"NAME: {agent.name}; age {seed.get('age', '?')}; job {agent.role}\n"
            f"Values: {'; '.join(p.get('values', []))}\nFears: {'; '.join(p.get('fears', []))}\n"
            f"Ambitions: {'; '.join(p.get('ambitions', []))}\nSecrets: {'; '.join(p.get('secrets', []))}\n"
            f"Speech: {p.get('speech_style', '')}\nQuirks: {'; '.join(p.get('quirks', []))}\n"
            f"Looks: {look.get('self_description', '')} Signature item: {look.get('signature_item', '')}\n"
            f"Backstory: {p.get('backstory', '')}")


def load_profiles(path: Path = PROFILES) -> dict[str, Profile]:
    if not path.exists():
        return {}
    return {p.name: p for p in Profiles.model_validate_json(path.read_text(encoding="utf-8")).profiles}


async def write_profiles(session: Session, settings: Settings, gateway: LLMGateway, *,
                         path: Path = PROFILES, redo: bool = False) -> dict[str, Profile]:
    """Portraits for everyone who has none yet (or everyone, with `redo`), saved to `path`."""
    known = {} if redo else load_profiles(path)
    missing = [a for a in session.scalars(select(Agent).order_by(Agent.id)) if a.name not in known]
    if missing:
        ws = session.get(WorldState, 1)
        tick = ws.tick if ws else 0
        req = LLMRequest(purpose="cast", model=settings.llm.models.sonnet, tier=ANGEL, effort=None,
                         system_rules=PROFILE_RULES, angel_role="reporter", max_tokens=9000,
                         messages=[ChatMessage(role="user", content="\n\n".join(_card(a) for a in missing)
                                               + "\n\nWrite the profiles. Reply with only the JSON.")],
                         context={"tick": tick})
        result = await gateway.call(req, tick=tick)
        cleaned = _FENCE.sub("", result.text.strip())
        try:
            got = Profiles.model_validate(json.loads(cleaned[cleaned.find("{"):cleaned.rfind("}") + 1]))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"the profiles came back unreadable: {str(exc)[:200]}") from exc
        known.update({p.name: p for p in got.profiles})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(Profiles(profiles=list(known.values())).model_dump_json(indent=1), encoding="utf-8")
    return known


def _ru(profiles: dict[str, Profile], agent: Agent) -> str:
    p = profiles.get(agent.name)
    return p.ru_name if p and p.ru_name else agent.name


def render_html(session: Session, settings: Settings, profiles: dict[str, Profile]) -> str:
    ws = session.get(WorldState, 1)
    assert ws is not None
    _, when = _when(settings, ws.tick)
    esc = html.escape
    people = list(session.scalars(select(Agent).order_by(Agent.id)))
    living = [a for a in people if a.alive]
    by_id = {a.id: a for a in people}
    locs = {loc.id: loc for loc in session.scalars(select(Location))}
    council = {a.id for a in elections.council(session, ws.tick)}
    owns: dict[int, list[str]] = {}
    for loc in locs.values():
        if loc.owner_id and economy.is_business(settings, loc):
            owns.setdefault(loc.owner_id, []).append(loc.name)
    bank = {acc.agent_id: acc for acc in session.scalars(select(BankAccount))}

    def name(a: Agent) -> str:
        return esc(_ru(profiles, a))

    rows = []
    for a in living:
        p = profiles.get(a.name, Profile(name=a.name))
        home = locs[a.home_id].name.replace("House No. ", "Дом\u00a0№") if a.home_id in locs else "—"
        acc = bank.get(a.id)
        money = f"{a.money_cents / 100:.2f}"
        if acc and acc.deposit_cents:
            money += f" (+{acc.deposit_cents / 100:.0f} в банке)"
        if acc and acc.loan_cents:
            money += f" (долг {acc.loan_cents / 100:.0f})"
        rows.append(f'<tr><td class="ph">{_face(a, 96)}</td><td><b>{name(a)}</b><br>'
                    f'<span class="role">{esc(a.name)}</span></td><td>{esc(p.who)}</td>'
                    f"<td>{esc(ROLES_RU.get(a.role, a.role))}"
                    f"{'<br><b>в совете</b>' if a.id in council else ''}</td>"
                    f"<td>{esc(', '.join(owns.get(a.id, [])) or '—')}</td><td>{esc(home)}</td>"
                    f"<td>{money}</td></tr>")

    cards = []
    for a in living:
        p = profiles.get(a.name, Profile(name=a.name))
        rels = list(session.scalars(select(Relationship).where(Relationship.agent_id == a.id)))
        known = [r for r in rels if r.other_id in by_id and by_id[r.other_id].alive]
        likes = sorted((r for r in known if r.affinity > 0.05), key=lambda r: -r.affinity)[:3]
        dislikes = sorted((r for r in known if r.affinity < -0.05), key=lambda r: r.affinity)[:3]
        owed = [r for r in known if r.debt_cents]

        def who_list(rs: list[Relationship]) -> str:
            return ", ".join(name(by_id[r.other_id]) for r in rs) or "—"

        debts = ", ".join(f"{name(by_id[r.other_id])} {'должен ему' if r.debt_cents > 0 else 'ему должен'} "
                          f"{abs(r.debt_cents) / 100:.2f}" for r in owed) or "—"
        seed = a.seed_card or {}
        cards.append(
            f'<div class="card person" id="a{a.id}">{_face(a)}<div class="info">'
            f"<b>{name(a)}</b><span class=\"role\">{esc(a.name)} · {seed.get('age', '?')} лет · "
            f"{esc(ROLES_RU.get(a.role, a.role))}</span>"
            f"<p><i>{esc(p.who)}</i></p>"
            f"<table class=\"kv\"><tr><td>Характер</td><td>{esc(p.character)}</td></tr>"
            f"<tr><td>Хочет</td><td>{esc(p.wants)}</td></tr>"
            f"<tr><td>Тайна</td><td>{esc(p.secret)}</td></tr>"
            f"<tr><td>Как говорит</td><td>{esc(p.voice)}</td></tr>"
            f"<tr><td>Узнать по</td><td>{esc(p.remember_by)}</td></tr>"
            f"<tr><td>Симпатии</td><td>{who_list(likes)}</td></tr>"
            f"<tr><td>Не любит</td><td>{who_list(dislikes)}</td></tr>"
            f"<tr><td>Долги</td><td>{debts}</td></tr></table></div></div>")

    dead = [f"<tr><td>{name(a)}</td><td>{esc(ROLES_RU.get(a.role, a.role))}</td>"
            f"<td>{esc(a.cause_of_death or '—')}</td></tr>" for a in people if not a.alive]
    extra = """
.ph .face,.ph svg{width:48px;height:48px;border-radius:50%;object-fit:cover}
th{text-align:left;font:600 12px system-ui,sans-serif;color:var(--soft);text-transform:uppercase;
letter-spacing:.08em;padding:6px 8px;border-bottom:2px solid var(--rule)}
.grid.people{grid-template-columns:repeat(auto-fill,minmax(min(100%,440px),1fr))}
.wide{overflow-x:auto}.wide table{min-width:720px}.wide td{vertical-align:top}
.person{flex-direction:column}.person>.face,.person>svg{width:120px;height:120px}
.kv td:first-child{color:var(--soft);white-space:nowrap;width:1%;padding-right:12px}
.kv td{font:14px/1.45 system-ui,sans-serif;vertical-align:top}
"""
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Кто есть кто в Асапвиле</title><style>{PAGE_STYLE}{extra}</style></head><body><main>"
            f'<header><div class="mast">Кто есть кто</div><div class="date">Асапвиль · обновлено: '
            f"{esc(when)}</div></header>"
            f'<h2 class="sec">Все жители</h2><div class="wide"><table><thead><tr><th></th><th>Имя</th>'
            f"<th>Кто это</th><th>Работа</th><th>Владеет</th><th>Живёт</th><th>Деньги</th></tr></thead>"
            f'{"".join(rows)}</table></div>'
            f'<h2 class="sec">Подробно</h2><div class="grid people">{"".join(cards)}</div>'
            + (f'<h2 class="sec">Умерли</h2><table>{"".join(dead)}</table>' if dead else "")
            + "<footer>Тайны жителей знают только читатели: сами горожане о них не знают.</footer>"
            "</main></body></html>")


PIN_FILE = PROFILES.with_name("cast-pin.txt")  # the message pinned last time, unpinned on refresh


def _telegram(method: str, body: dict, token: str) -> dict:
    import urllib.request

    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def pin(message_id: int, *, token: str, chat: str, pin_file: Path = PIN_FILE) -> dict:
    """Pin the new page and unpin the one before it, so only the latest stays on top."""
    res = _telegram("pinChatMessage", {"chat_id": chat, "message_id": message_id,
                                       "disable_notification": True}, token)
    if res.get("ok"):
        if pin_file.exists() and pin_file.read_text().strip() not in ("", str(message_id)):
            try:
                old = int(pin_file.read_text())
                _telegram("unpinChatMessage", {"chat_id": chat, "message_id": old}, token)
            except Exception:  # the old pin is gone already: nothing to undo
                pass
        pin_file.write_text(str(message_id))
    return res
