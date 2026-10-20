"""Needs decay and their physical consequences.

All needs are 0..100. Hunger rises toward 100 (starving); energy, social and
health fall toward 0. Unmet needs hurt mood, then health; health 0 is death.
Pure functions over a small dataclass so the rules are trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from asapville.config import NeedsCfg


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class NeedsState:
    hunger: float
    energy: float
    social: float
    health: float
    mood: float


@dataclass
class NeedsOutcome:
    state: NeedsState
    collapsed: bool = False  # energy hit zero: the body forces sleep
    died: bool = False
    cause_of_death: str | None = None
    notes: list[str] = field(default_factory=list)


def tick_needs(
    s: NeedsState,
    cfg: NeedsCfg,
    *,
    asleep: bool,
    at_home: bool = True,
    resting: bool = False,
    socialised: bool = False,
    exposure_damage: float = 0.0,  # weather / injury hook (Phase 5)
) -> NeedsOutcome:
    """Advance one tick of bodily needs. Returns a new state; input is not mutated."""
    hunger, energy, social, health = s.hunger, s.energy, s.social, s.health
    notes: list[str] = []

    if asleep:
        hunger += cfg.hunger_per_tick_asleep
        energy += cfg.energy_per_tick_sleep_home if at_home else cfg.energy_per_tick_sleep_rough
    else:
        hunger += cfg.hunger_per_tick_awake
        energy += cfg.energy_per_tick_rest if resting else -cfg.energy_per_tick_awake
        social += cfg.social_gain_talk if socialised else -cfg.social_per_tick_awake

    hunger, energy, social = clamp(hunger), clamp(energy), clamp(social)

    damage = exposure_damage
    if hunger >= 100.0:
        damage += cfg.famished_damage
        notes.append("famished")
    elif hunger >= cfg.starving_threshold:
        damage += cfg.starving_damage
        notes.append("starving")
    collapsed = False
    if not asleep and energy <= cfg.collapse_energy:
        damage += cfg.exhaustion_damage
        collapsed = True
        notes.append("collapsed from exhaustion")
    if damage > 0:
        health -= damage
    elif hunger < 50 and energy > 30:
        health += cfg.health_regen
    health = clamp(health)

    mood = compute_mood(hunger, energy, social, health)
    if asleep and not at_home:
        mood = clamp(mood - cfg.rough_sleep_mood_penalty)

    died = health <= 0.0
    cause = None
    if died:
        cause = "starvation" if hunger >= cfg.starving_threshold else (
            "exhaustion" if collapsed else "failing health")
    return NeedsOutcome(
        state=NeedsState(hunger=hunger, energy=energy, social=social, health=health, mood=mood),
        collapsed=collapsed, died=died, cause_of_death=cause, notes=notes,
    )


def compute_mood(hunger: float, energy: float, social: float, health: float) -> float:
    """Baseline mood from bodily state (events add their own swings later)."""
    satiety = 100.0 - hunger
    return clamp(0.30 * satiety + 0.25 * energy + 0.20 * social + 0.25 * health)


def eat(hunger: float, nutrition: float) -> float:
    return clamp(hunger - nutrition)


def describe_needs(hunger: float, energy: float, social: float, health: float) -> list[str]:
    """How the body feels, in words (perception never exposes raw numbers alone)."""
    out = []
    if hunger >= 90:
        out.append("you are starving; your hands shake")
    elif hunger >= 65:
        out.append("you are very hungry")
    elif hunger >= 40:
        out.append("you are getting hungry")
    elif hunger <= 20:
        out.append("you are well fed")
    if energy <= 10:
        out.append("you are about to collapse from exhaustion")
    elif energy <= 30:
        out.append("you are tired")
    if social <= 20:
        out.append("you feel lonely")
    if health <= 30:
        out.append("you feel seriously unwell")
    elif health <= 60:
        out.append("you feel a bit unwell")
    return out
