"""Persona block: an agent's fixed traits rendered as behavioural instructions.

Every citizen LLM call starts with this block. It is deterministic for a given
personality (stable bytes -> stable prompt cache).
"""

from __future__ import annotations

from asapville.agents.schemas import Personality

# (trait, high phrase, low phrase). Middle values say nothing: average is unremarkable.
_BIG_FIVE = {
    "openness": ("You are curious, imaginative and drawn to new ideas and strangers.",
                 "You are conventional and suspicious of anything new; you like things as they were."),
    "conscientiousness": ("You are disciplined, punctual and keep your promises; mess irritates you.",
                          "You are impulsive and disorganised; you forget promises and cut corners."),
    "extraversion": ("You are loud and sociable; you seek company and talk first.",
                     "You are reserved and private; you avoid crowds and speak only when needed."),
    "agreeableness": ("You are warm and trusting; you hate conflict and help people unasked.",
                      "You are blunt, stubborn and distrustful; you don't care if people like you."),
    "neuroticism": ("You worry constantly, take slights personally and lose your temper under stress.",
                    "You are calm and hard to rattle, even when things go wrong."),
}

_DARK = {
    "machiavellianism": "You manipulate people when it serves you, and you plan several moves ahead.",
    "narcissism": "You believe you deserve more than others and crave admiration; criticism stings.",
    "psychopathy": "You feel little guilt or empathy; other people's pain doesn't stop you.",
}

_ALIGNMENT = {
    "lawful-good": "You try to do the right thing and respect the town's laws, even at a cost to yourself.",
    "neutral": "You look after yourself and your own, bending rules when nobody gets hurt.",
    "selfish": "Your own interest comes first. You break rules if you think you won't be caught.",
    "cruel": "You enjoy power over others and you are willing to hurt people to get it.",
    "criminal": "You see laws as obstacles. Theft, lies and intimidation are tools you use.",
}


def _list(items: list[str]) -> str:
    return "; ".join(items) if items else "none worth mentioning"


def trait_lines(p: Personality) -> list[str]:
    lines: list[str] = []
    for trait, (high, low) in _BIG_FIVE.items():
        v = getattr(p.big_five, trait)
        if v >= 0.65:
            lines.append(high)
        elif v <= 0.35:
            lines.append(low)
    for trait, phrase in _DARK.items():
        if getattr(p.dark_traits, trait) >= 0.6:
            lines.append(phrase)
    lines.append(_ALIGNMENT[p.moral_alignment])
    return lines


def persona_block(name: str, p: Personality, *, age: int | None = None,
                  role: str | None = None) -> str:
    a = p.appearance
    who = f"You are {name}" + (f", {age} years old" if age else "") + (
        f", currently {role.replace('_', ' ')}" if role else "") + ", a resident of Asapville."
    parts = [
        "## WHO YOU ARE",
        who,
        "You are a real person living your own life. Stay in character at all times.",
        "",
        "## YOUR NATURE (fixed; act on it)",
        *[f"- {line}" for line in trait_lines(p)],
        f"- Values: {_list(p.values)}.",
        f"- Fears: {_list(p.fears)}.",
        f"- Ambitions: {_list(p.ambitions)}.",
        f"- Secrets (never volunteer these): {_list(p.secrets)}.",
        f"- Quirks: {_list(p.quirks)}.",
        "",
        "## HOW YOU SPEAK",
        p.speech_style,
        "",
        "## HOW YOU LOOK (your own choice)",
        f"{a.self_description} Hair: {a.hair}. Build: {a.build}. Clothing: {a.clothing}. "
        f"Never without: {a.signature_item}.",
        "",
        "## YOUR STORY",
        p.backstory,
    ]
    return "\n".join(parts)


def short_looks(p: Personality) -> str:
    """What a stranger sees: build, hair, clothing, signature item. Never the name."""
    a = p.appearance
    return f"{a.build}, {a.hair} hair, wearing {a.clothing}, with {a.signature_item}"


def unpleasant_reasons(p: Personality) -> list[str]:
    """Why an observer would call this person unpleasant (empty list = they're not)."""
    reasons = []
    if p.moral_alignment in ("selfish", "cruel", "criminal"):
        reasons.append(f"{p.moral_alignment} moral baseline")
    for trait in ("machiavellianism", "narcissism", "psychopathy"):
        if getattr(p.dark_traits, trait) >= 0.6:
            reasons.append(f"high {trait}")
    if p.big_five.agreeableness <= 0.25:
        reasons.append("very disagreeable")
    return reasons
