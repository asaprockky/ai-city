"""FakeLLM: a $0 backend returning canned JSON, deterministic per (agent, tick).

- `script(purpose, *texts)` queues exact replies (tests use this for bad JSON etc.).
- Otherwise a per-purpose handler fabricates a plausible reply from `req.context`.
Token counts are estimated (chars/4) and a repeated static prefix is reported as
cache reads, so token accounting and the cache-hit panel can be exercised for free.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from asapville.llm.types import LLMRequest, RawCompletion

Handler = Callable[[LLMRequest, random.Random], str]

# Canned self-portraits used for FakeLLM births. One per seed index (cycled).
FAKE_PORTRAITS: list[dict[str, Any]] = [
    {"name": "Mara Quill", "values": ["honesty", "hard work"], "fears": ["debt"],
     "ambitions": ["open a bakery"], "secrets": ["she once burned down a barn by accident"],
     "speech_style": "Warm and precise; she asks questions before giving opinions.",
     "quirks": ["hums while working"],
     "appearance": {"hair": "auburn braid", "build": "sturdy", "clothing": "flour-dusted apron",
                    "signature_item": "a brass thimble on a cord",
                    "self_description": "A sturdy woman with flour on her sleeves."},
     "backstory": "Raised by a widowed miller, Mara learned that bread is trust."},
    {"name": "Dorran Vex", "values": ["power", "winning"], "fears": ["being laughed at"],
     "ambitions": ["own half the town"], "secrets": ["he cheats at cards", "he owes a debt he hides"],
     "speech_style": "Short, cutting sentences. Swears when stressed. Never apologises.",
     "quirks": ["cracks his knuckles before lying"],
     "appearance": {"hair": "slicked black", "build": "lean", "clothing": "a long grey coat",
                    "signature_item": "a silver toothpick",
                    "self_description": "A lean man who watches everyone's hands."},
     "backstory": "Dorran grew up stealing from market stalls and decided he would never be poor again."},
    {"name": "Ilse Brandt", "values": ["family", "faith"], "fears": ["fire"],
     "ambitions": ["see her children educated"], "secrets": [],
     "speech_style": "Soft-spoken, old-fashioned turns of phrase, quotes proverbs.",
     "quirks": ["counts things under her breath"],
     "appearance": {"hair": "grey bun", "build": "small", "clothing": "a dark wool shawl",
                    "signature_item": "a wooden rosary",
                    "self_description": "A small woman with sharp, kind eyes."},
     "backstory": "Ilse buried two husbands and still gets up before dawn."},
    {"name": "Tobin Reyes", "values": ["freedom", "fun"], "fears": ["being tied down"],
     "ambitions": ["become famous for something"], "secrets": ["he can't read well"],
     "speech_style": "Fast, jokey, full of exaggerations and nicknames for everyone.",
     "quirks": ["whistles off-key"],
     "appearance": {"hair": "curly brown", "build": "lanky", "clothing": "a patched red jacket",
                    "signature_item": "a battered harmonica",
                    "self_description": "A lanky grin in a red jacket."},
     "backstory": "Tobin drifted into Asapville after his band broke up in a bar fight."},
    {"name": "Sela Okafor", "values": ["knowledge", "justice"], "fears": ["corruption"],
     "ambitions": ["run the newspaper"], "secrets": ["she reads other people's letters"],
     "speech_style": "Measured and formal; she uses exact numbers and dislikes vagueness.",
     "quirks": ["taps her pencil when thinking"],
     "appearance": {"hair": "close-cropped", "build": "tall", "clothing": "a pressed blue blouse",
                    "signature_item": "a pencil behind her ear",
                    "self_description": "A tall woman who looks like she's taking notes."},
     "backstory": "Sela left the city after her exposé got her fired."},
]

# Location keys the fake wanderer likes to visit.
_WANDER = ["square", "market", "tavern", "church", "forest", "river", "farm"]


def _stable_rng(*parts: Any) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def fake_birth(req: LLMRequest, rng: random.Random) -> str:
    idx = int(req.context.get("seed_index", 0))
    portrait = dict(FAKE_PORTRAITS[idx % len(FAKE_PORTRAITS)])
    if idx >= len(FAKE_PORTRAITS):
        portrait["name"] = f"{portrait['name']} {idx // len(FAKE_PORTRAITS) + 1}"
    return json.dumps(portrait)


def fake_decide(req: LLMRequest, rng: random.Random) -> str:
    """A crude but needs-aware citizen: eat when hungry, forage, rest, chat, wander."""
    p = req.context.get("perception", {})
    inv: dict[str, int] = p.get("inventory", {})
    food = [k for k, v in inv.items() if v > 0 and k in {"bread", "berries", "fish", "vegetables"}]
    here = [a for a in p.get("here", []) if not a.get("asleep")]
    dests = {d["key"]: d for d in p.get("destinations", [])}
    loc = p.get("location_key")

    def out(thought: str, action: str, args: dict | None = None, dialogue: str | None = None,
            mood: str = "calm") -> str:
        return json.dumps({"thought": thought, "action": action, "action_args": args or {},
                           "dialogue": dialogue, "mood": mood})

    if p.get("hunger", 0) >= 45 and food:
        return out("My stomach is growling.", "eat", {"item": food[0]}, mood="hungry")
    if p.get("hunger", 0) >= 55 and not food:
        if loc in ("forest", "river"):
            return out("Maybe I can find something to eat here.", "forage", mood="worried")
        target = min((k for k in ("forest", "river") if k in dests),
                     key=lambda k: dests[k]["travel_hours"], default=None)
        if target:
            return out("I need food. The wild might provide.", "move", {"to": target}, mood="anxious")
    if p.get("energy", 100) <= 25:
        return out("I'm worn out.", "rest", mood="tired")
    if here and rng.random() < 0.4:
        other = rng.choice(here)
        greeting = "Hello there." if other.get("name") is None else f"Good to see you, {other['name']}."
        return out("I could use some company.", "talk", {"to": other["ref"]}, greeting, "friendly")
    if rng.random() < 0.5:
        options = [k for k in _WANDER if k in dests and k != loc]
        if options:
            return out("Let's see what's happening elsewhere.", "move", {"to": rng.choice(options)})
    return out("Nothing pressing right now.", "idle")


class FakeLLM:
    name = "fake"

    def __init__(self, *, billable: bool = False, handlers: dict[str, Handler] | None = None) -> None:
        self.billable = billable  # True lets tests exercise the budget guard with fake spend
        self.handlers: dict[str, Handler] = {"birth": fake_birth, "decide": fake_decide}
        if handlers:
            self.handlers.update(handlers)
        self._scripts: dict[str, deque[str]] = defaultdict(deque)
        self._seen_prefixes: set[str] = set()
        self.requests: list[LLMRequest] = []

    def script(self, purpose: str, *texts: str) -> None:
        self._scripts[purpose].extend(texts)

    async def complete(self, req: LLMRequest) -> RawCompletion:
        self.requests.append(req)
        rng = _stable_rng(req.purpose, req.agent_id, req.context.get("tick"), len(req.messages))
        if self._scripts[req.purpose]:
            text = self._scripts[req.purpose].popleft()
        elif req.purpose in self.handlers:
            text = self.handlers[req.purpose](req, rng)
        else:
            text = "{}"
        prefix = req.system_rules + (req.system_persona or "")
        dynamic = "".join(m.content for m in req.messages)
        prefix_tokens = _est_tokens(prefix)
        cached = prefix in self._seen_prefixes
        self._seen_prefixes.add(prefix)
        return RawCompletion(
            text=text,
            input_tokens=_est_tokens(dynamic),
            output_tokens=_est_tokens(text),
            cache_read_tokens=prefix_tokens if cached else 0,
            cache_write_tokens=0 if cached else prefix_tokens,
            stop_reason="end_turn",
        )
