"""Asapville in manga: a page of panels drawn from what really happened in town.

One LLM call (the stronger model) reads the hours' events and writes a short script in
Russian: a title and four to six panels, each with a caption, a setting and speech bubbles
by real citizens. The page is drawn in code (black ink, screentone, speech bubbles) with a
fixed face for every citizen, built from their own appearance, so they look the same in
every chapter. Headless Chromium turns it into a PNG; `post_telegram` sends it to a channel.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from asapville.agents.schemas import Personality
from asapville.db.models import Agent, Event, Location
from asapville.llm.client import LLMGateway
from asapville.llm.routing import ANGEL
from asapville.llm.types import ChatMessage, LLMRequest

CHROMIUM = os.environ.get("ASAPVILLE_CHROMIUM", "/opt/pw-browsers/chromium")
FACES = Path(__file__).resolve().parent.parent / "manga" / "faces"  # <agent id>.jpg|png portraits
SKIP_KINDS = {"move", "rest", "sleep", "idle", "travel", "depart", "arrive", "wake", "plan",
              "invalid_action", "tier_change", "angel"}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

SCRIPT_RULES = """You are the writer of a weekly manga about Asapville, a small town whose \
citizens live their own lives. You get the real events of the last hours. Write ONE manga page \
about the most dramatic or touching thread in them: a scheme, a rivalry, a kindness, a bargain, \
an election. Stay true to the events and to who the people are; do not invent deaths or deeds \
that did not happen. Write all text in Russian, short and punchy like a real manga.

Reply with ONLY a JSON object:
{"title": "<chapter title>",
 "panels": [
   {"caption": "<narrator box, one short line, or empty>",
    "setting": "<where, one or two words>",
    "characters": ["<exact citizen name>", ...  at most 3],
    "bubbles": [{"who": "<exact citizen name>", "text": "<what they say, at most 90 characters>"}],
    "sfx": "<a sound effect in Russian, or empty>"}
 ]}
Use 4 to 6 panels and at most 2 bubbles per panel. The last panel should leave the reader \
wanting the next chapter."""


class Bubble(BaseModel):
    who: str
    text: str = Field(max_length=160)


class Panel(BaseModel):
    caption: str = ""
    setting: str = ""
    characters: list[str] = Field(default_factory=list)
    bubbles: list[Bubble] = Field(default_factory=list)
    sfx: str = ""


class Script(BaseModel):
    title: str
    panels: list[Panel] = Field(min_length=1, max_length=8)


def digest(session: Session, since_tick: int, until_tick: int, limit: int = 120) -> str:
    """The hours' events, one line each, for the writer."""
    rows = session.scalars(select(Event).where(Event.tick >= since_tick, Event.tick <= until_tick,
                                               Event.kind.not_in(SKIP_KINDS)).order_by(Event.id))
    lines = []
    for e in rows:
        if e.kind in ("work", "eat"):
            continue
        lines.append(f"[{e.tick}] {e.kind}: {e.summary[:260]}")
    return "\n".join(lines[-limit:])


def cast_list(session: Session) -> str:
    out = []
    for a in session.scalars(select(Agent).where(Agent.alive.is_(True)).order_by(Agent.id)):
        p = Personality.model_validate(a.personality)
        out.append(f"- {a.name}, {a.role}: {p.appearance.self_description[:140]}")
    return "\n".join(out)


def parse_script(text: str) -> Script:
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    try:
        return Script.model_validate(json.loads(cleaned[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(str(exc)[:300]) from exc


async def write_script(session: Session, gateway: LLMGateway, model: str, since_tick: int,
                       until_tick: int) -> Script:
    prompt = (f"The citizens:\n{cast_list(session)}\n\nEvents (hour: what happened):\n"
              f"{digest(session, since_tick, until_tick)}\n\nWrite the page. Reply with only the JSON.")
    req = LLMRequest(purpose="comic", model=model, tier=ANGEL, effort=None, system_rules=SCRIPT_RULES,
                     messages=[ChatMessage(role="user", content=prompt)], max_tokens=2000,
                     angel_role="mangaka", context={"since": since_tick, "until": until_tick})
    result = await gateway.call(req, tick=until_tick)
    return parse_script(result.text)


# ------------------------------------------------------------------ drawing
_HAIR_TONES = [("white", "#f2f2f2"), ("silver", "#c8c8c8"), ("grey", "#b0b0b0"), ("gray", "#b0b0b0"),
               ("blond", "#e8e0c8"), ("fair", "#e8e0c8"), ("red", "#8a8a8a"), ("auburn", "#7a7a7a"),
               ("brown", "#555"), ("dark", "#2a2a2a"), ("black", "#111")]


def _hair_tone(hair: str) -> str:
    h = hair.lower()
    for word, tone in _HAIR_TONES:
        if word in h:
            return tone
    return "#444"


def portrait(agent: Agent, size: int = 120) -> str:
    """The citizen's drawn portrait if one exists (manga/faces/<id>.jpg), else their ink bust."""
    import base64

    for ext in ("jpg", "png"):
        f = FACES / f"{agent.id}.{ext}"
        if f.exists():
            data = base64.b64encode(f.read_bytes()).decode()
            return (f'<img class="face" src="data:image/{"jpeg" if ext == "jpg" else "png"};base64,{data}" '
                    f'width="{size}" height="{size}">')
    return face_svg(agent, size)


def face_svg(agent: Agent, size: int = 120) -> str:
    """A citizen's manga bust, the same every time: hair tone from their own words, style from their name."""
    p = Personality.model_validate(agent.personality)
    seed = int(hashlib.sha1(agent.name.encode()).hexdigest(), 16)
    tone = _hair_tone(p.appearance.hair)
    style = seed % 4
    broad = any(w in p.appearance.build.lower() for w in ("broad", "big", "stout", "thick", "heavy"))
    w = 46 if broad else 40
    hair = {
        0: f'<path d="M{60-w} 58 Q60 0 {60+w} 58 Q{60+w-8} 30 60 26 Q{60-w+8} 30 {60-w} 58Z" fill="{tone}"/>',
        1: f'<path d="M{60-w} 70 Q{60-w-6} 10 60 14 Q{60+w+6} 10 {60+w} 70 L{60+w-6} 44 Q60 30 {60-w+6} 44Z" '
           f'fill="{tone}"/>',
        2: f'<path d="M{60-w+2} 52 Q60 8 {60+w-2} 52 Q72 34 60 40 Q48 34 {60-w+2} 52Z" fill="{tone}"/>'
           f'<path d="M{60+w-4} 40 q14 30 4 64" stroke="{tone}" stroke-width="10" fill="none"/>',
        3: f'<path d="M{60-w} 56 Q50 4 60 6 Q70 4 {60+w} 56 L{60+w-10} 40 L64 30 L56 40 L48 30 Z" '
           f'fill="{tone}"/>',
    }[style]
    eyes = ('<ellipse cx="46" cy="66" rx="6" ry="8" fill="#111"/>'
            '<ellipse cx="74" cy="66" rx="6" ry="8" fill="#111"/>'
            '<circle cx="48" cy="63" r="2" fill="#fff"/><circle cx="76" cy="63" r="2" fill="#fff"/>')
    sly = p.dark_traits.machiavellianism > 0.6
    brows = ('<path d="M38 54 L54 58 M82 54 L66 58" stroke="#111" stroke-width="3"/>' if sly else
             '<path d="M38 56 Q46 52 54 56 M66 56 Q74 52 82 56" stroke="#111" '
             'stroke-width="2.5" fill="none"/>')
    mouth = ('<path d="M50 90 Q60 95 72 87" stroke="#111" stroke-width="2.5" fill="none"/>' if sly else
             '<path d="M52 89 Q60 95 68 89" stroke="#111" stroke-width="2.5" fill="none"/>')
    return (f'<svg viewBox="0 0 120 130" width="{size}" height="{size * 130 // 120}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<path d="M14 130 Q20 100 60 104 Q100 100 106 130Z" fill="#fff" stroke="#111" stroke-width="3"/>'
            f'<ellipse cx="60" cy="68" rx="{w - 6}" ry="40" fill="#fff" stroke="#111" stroke-width="3"/>'
            f'{hair}{brows}{eyes}{mouth}</svg>')


PAGE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#fff;width:1080px;font-family:'DejaVu Sans',sans-serif;color:#111}
.page{padding:28px;background:#fff}
.title{font-family:'DejaVu Serif',serif;font-weight:bold;font-size:44px;text-align:center;
  border:5px solid #111;padding:14px;margin-bottom:18px;letter-spacing:1px;
  background:repeating-linear-gradient(45deg,#fff 0 6px,#eee 6px 8px)}
.sub{font-size:18px;text-align:center;margin:-8px 0 18px;letter-spacing:3px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.panel{position:relative;border:5px solid #111;min-height:470px;overflow:hidden;display:flex;
  flex-direction:column;justify-content:space-between;
  background:radial-gradient(#ddd 1.2px,transparent 1.3px) 0 0/9px 9px,#fff}
.panel.wide{grid-column:span 2;min-height:400px}
.cap{align-self:flex-start;background:#111;color:#fff;font-size:19px;padding:8px 14px;max-width:74%;
  line-height:1.3}
.set{position:absolute;top:8px;right:12px;font-size:14px;letter-spacing:2px;color:#555;background:#fff;
  padding:0 6px}
.bubbles{display:flex;flex-direction:column;gap:14px;padding:12px 14px 0;position:relative;z-index:2}
.b{position:relative;background:#fff;border:3px solid #111;border-radius:26px;padding:10px 16px;
  font-size:19px;line-height:1.3;max-width:90%;align-self:flex-start}
.b:nth-child(2){align-self:flex-end}
.b small{display:block;font-size:13px;letter-spacing:1px;color:#555;margin-bottom:2px}
.b:after{content:'';position:absolute;bottom:-17px;left:40px;border:9px solid transparent;
  border-top:13px solid #111}
.b:nth-child(2):after{left:auto;right:40px}
.cast{display:flex;justify-content:space-around;align-items:flex-end;padding-top:6px}
.who{text-align:center;font-size:15px;font-weight:bold}
.face{display:block;border:3px solid #111;border-bottom:none;filter:grayscale(1) contrast(1.1);
  object-fit:cover;background:#fff}
.who span{display:inline-block;background:#fff;border:2px solid #111;padding:1px 8px;margin-top:-6px}
.sfx{position:absolute;right:16px;bottom:150px;font-family:'DejaVu Serif',serif;font-weight:bold;
  font-size:50px;transform:rotate(-12deg);color:#fff;-webkit-text-stroke:3px #111;z-index:1}
.foot{text-align:center;font-size:16px;margin-top:16px;letter-spacing:2px}
"""


def render_html(session: Session, script: Script, *, chapter: str = "") -> str:
    by_name = {a.name: a for a in session.scalars(select(Agent))}
    first = {a.name.split()[0]: a for a in by_name.values()}

    def find(name: str) -> Agent | None:
        return by_name.get(name) or first.get(name.split()[0] if name else "")

    panels = []
    n = len(script.panels)
    for i, p in enumerate(script.panels):
        wide = n % 2 == 1 and i == n - 1
        cast = [a for a in (find(c) for c in p.characters[:3]) if a is not None]
        faces = "".join(f'<div class="who">{portrait(a, 170 if len(cast) < 3 else 135)}'
                        f'<br><span>{html.escape(a.name.split()[0])}</span></div>' for a in cast)
        bubbles = "".join(
            f'<div class="b"><small>{html.escape((find(b.who).name.split()[0]) if find(b.who) else b.who)}'
            f'</small>{html.escape(b.text)}</div>' for b in p.bubbles[:2])
        panels.append(
            f'<div class="panel{" wide" if wide else ""}">'
            + f'<div>{(f"<div class=cap>{html.escape(p.caption)}</div>" if p.caption else "")}'
            + f'<div class="bubbles">{bubbles}</div></div>'
            + (f'<div class="sfx">{html.escape(p.sfx)}</div>' if p.sfx and len(p.bubbles) < 2 else "")
            + f'<div class="cast">{faces}</div>'
            + (f'<div class="set">{html.escape(p.setting.upper())}</div>' if p.setting else "")
            + "</div>")
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{PAGE_CSS}</style></head><body>'
            f'<div class="page"><div class="title">{html.escape(script.title)}</div>'
            f'<div class="sub">АСАПВИЛЬ {html.escape(chapter)}</div>'
            f'<div class="grid">{"".join(panels)}</div>'
            f'<div class="foot">продолжение следует</div></div></body></html>')


def render_png(page_html: str, out: Path) -> Path:
    src = out.with_suffix(".html")
    src.write_text(page_html, encoding="utf-8")
    subprocess.run([CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=1080,4000", f"--screenshot={out}", src.resolve().as_uri()],
                   check=True, capture_output=True, timeout=120)
    _trim(out)
    return out


def _trim(png: Path, margin: int = 24) -> None:
    """Cut the blank page below the drawing."""
    from PIL import Image, ImageOps

    im = Image.open(png).convert("RGB")
    box = ImageOps.invert(im).getbbox()
    if box:
        im.crop((0, 0, im.width, min(im.height, box[3] + margin))).save(png)


GEMINI_MODELS = [m for m in (os.environ.get("ASAPVILLE_GEMINI_IMAGE_MODEL"), "gemini-3.1-flash-lite-image",
                               "gemini-3.1-flash-image",
                               "gemini-2.5-flash-image") if m]
PORTRAIT_STYLE = ("Black-and-white Japanese manga character portrait, head and shoulders, clean ink lines, "
                  "screentone shading, plain white background, seinen manga style, no text, no lettering.")


def portrait_prompt(agent: Agent) -> str:
    p = Personality.model_validate(agent.personality)
    a = p.appearance
    age = (agent.seed_card or {}).get("age")
    return (f"{PORTRAIT_STYLE} {a.self_description} {f'About {age} years old. ' if age else ''}"
            f"Hair: {a.hair}. Build: {a.build}. Clothing: {a.clothing}. Always with: {a.signature_item}.")


def has_portrait(agent: Agent) -> bool:
    return any((FACES / f"{agent.id}.{ext}").exists() for ext in ("jpg", "png"))


def draw_portrait(agent: Agent, *, key: str) -> Path:
    """Ask Gemini for this citizen's portrait and keep it in manga/faces/<id>.png."""
    import base64

    body = json.dumps({"contents": [{"parts": [{"text": portrait_prompt(agent)}]}],
                       "generationConfig": {"responseModalities": ["IMAGE"]}}).encode()
    last = ""
    for model in GEMINI_MODELS:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            last = f"{model}: {exc.code} {exc.read()[:200]!r}"
            continue
        for part in (data.get("candidates") or [{}])[0].get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                FACES.mkdir(parents=True, exist_ok=True)
                out = FACES / f"{agent.id}.png"
                out.write_bytes(base64.b64decode(inline["data"]))
                return out
        last = f"{model}: no image in the reply"
    raise RuntimeError(f"Gemini drew nothing for {agent.name} ({last})")


def draw_missing_portraits(session: Session, *, key: str) -> list[str]:
    done = []
    for a in session.scalars(select(Agent).where(Agent.alive.is_(True)).order_by(Agent.id)):
        if not has_portrait(a):
            draw_portrait(a, key=key)
            done.append(a.name)
    return done


def post_telegram(png: Path, caption: str, *, token: str, chat: str) -> dict[str, Any]:
    """Send the page to a Telegram chat or channel (the bot must be an admin there)."""
    boundary = "asapville" + hashlib.sha1(png.read_bytes()[:64]).hexdigest()[:16]
    parts = []
    for name, value in (("chat_id", chat), ("caption", caption[:1024])):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
                     .encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{png.name}"\r\n'
                 f"Content-Type: image/png\r\n\r\n".encode() + png.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendPhoto", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def location_name(session: Session, key: str) -> str:
    loc = session.scalar(select(Location).where(Location.key == key))
    return loc.name if loc else key
