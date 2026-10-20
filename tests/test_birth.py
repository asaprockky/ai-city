import random

from sqlalchemy import select

from asapville.agents.birth import DARK_ALIGNMENTS, roll_seed_card
from asapville.agents.persona import persona_block
from asapville.agents.schemas import Personality
from asapville.db.models import Agent, Brand


def test_seed_cards_allow_about_15_percent_dark(settings):
    rng = random.Random(0)
    cards = [roll_seed_card(rng, settings) for _ in range(2000)]
    share = sum(c.moral_alignment in DARK_ALIGNMENTS for c in cards) / len(cards)
    assert 0.11 < share < 0.19
    dark = [c for c in cards if c.moral_alignment in DARK_ALIGNMENTS]
    good = [c for c in cards if c.moral_alignment not in DARK_ALIGNMENTS]
    avg = lambda xs, f: sum(map(f, xs)) / len(xs)  # noqa: E731
    assert avg(dark, lambda c: c.dark_traits.psychopathy) > avg(good, lambda c: c.dark_traits.psychopathy)


async def test_births_produce_valid_unique_citizens(world, sessions):
    with sessions() as s:
        agents = list(s.scalars(select(Agent)))
        assert len({a.virtual_mac for a in agents}) == len({a.virtual_ip for a in agents}) == 5
        for a in agents:
            p = Personality.model_validate(a.personality)
            assert a.virtual_mac.startswith("02:")
            assert a.home_id is not None and a.location_id == a.home_id
            assert p.appearance.signature_item
        assert s.scalar(select(Brand).where(Brand.name == "AsapBank")) is not None


async def test_persona_block_renders_dark_traits(world, sessions):
    with sessions() as s:
        a = s.get(Agent, 1)
        p = Personality.model_validate(a.personality).model_copy(deep=True)
    p.dark_traits.psychopathy = 0.9
    p.big_five.agreeableness = 0.1
    p.moral_alignment = "cruel"
    block = persona_block("Test Person", p, age=40)
    assert "little guilt" in block and "willing to hurt people" in block
    assert "blunt, stubborn" in block and "Test Person, 40 years old" in block
    assert persona_block("Test Person", p, age=40) == block  # stable bytes for caching
