from asapville.config import NeedsCfg
from asapville.world.needs import NeedsState, compute_mood, eat, tick_needs

CFG = NeedsCfg()


def s(**kw) -> NeedsState:
    base = dict(hunger=20.0, energy=80.0, social=60.0, health=100.0, mood=60.0)
    base.update(kw)
    return NeedsState(**base)


def test_awake_decay():
    out = tick_needs(s(), CFG, asleep=False)
    assert out.state.hunger == 20 + CFG.hunger_per_tick_awake
    assert out.state.energy == 80 - CFG.energy_per_tick_awake
    assert out.state.social == 60 - CFG.social_per_tick_awake


def test_sleep_restores_more_at_home_than_rough():
    home = tick_needs(s(energy=30), CFG, asleep=True, at_home=True)
    rough = tick_needs(s(energy=30), CFG, asleep=True, at_home=False)
    assert home.state.energy > rough.state.energy > 30
    assert home.state.mood > rough.state.mood
    assert home.state.hunger == 20 + CFG.hunger_per_tick_asleep


def test_starvation_damages_then_kills():
    st, died, ticks = s(hunger=99.0, health=10.0), False, 0
    while not died and ticks < 50:
        out = tick_needs(st, CFG, asleep=False)
        st, died, ticks = out.state, out.died, ticks + 1
    assert died and out.cause_of_death == "starvation"
    assert ticks == 3  # 10 hp at famished_damage 4/tick


def test_exhaustion_collapses():
    out = tick_needs(s(energy=2.0), CFG, asleep=False)
    assert out.collapsed and out.state.health < 100


def test_regen_only_when_fed_and_rested():
    hurt = s(health=50.0)
    assert tick_needs(hurt, CFG, asleep=False).state.health > 50
    assert tick_needs(s(health=50.0, hunger=85.0), CFG, asleep=False).state.health < 50


def test_rest_and_talk():
    assert tick_needs(s(), CFG, asleep=False, resting=True).state.energy > 80
    assert tick_needs(s(), CFG, asleep=False, socialised=True).state.social > 60


def test_eat_and_mood_bounds():
    assert eat(30, 35) == 0
    assert 0 <= compute_mood(100, 0, 0, 0) <= compute_mood(0, 100, 100, 100) <= 100
