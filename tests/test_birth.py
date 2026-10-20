import random

from sqlalchemy import select

from asapville.agents.birth import DARK_ALIGNMENTS, roll_founders, roll_seed_card
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


def test_founders_always_include_min_dark(settings):
    settings.birth.min_dark = 2
    for seed in range(30):
        cards = roll_founders(random.Random(seed), settings, 10)
        assert sum(c.moral_alignment in DARK_ALIGNMENTS for c in cards) >= 2


def test_describe_seed_uses_words(settings):
    from asapville.agents.birth import describe_seed

    card = roll_seed_card(random.Random(3), settings)
    text = describe_seed(card)
    assert "agreeableness" in text and "Moral baseline" in text and str(card.age) in text


async def test_duplicate_name_is_rejected_and_retried(settings, sessions, fake, gateway):
    import json

    from asapville.agents.birth import write_self
    from asapville.llm.fake import FAKE_PORTRAITS

    dup = json.dumps(FAKE_PORTRAITS[0])
    fresh = json.dumps({**FAKE_PORTRAITS[0], "name": "Brand New"})
    fake.script("birth", dup, fresh)
    card = roll_seed_card(random.Random(1), settings)
    portrait = await write_self(gateway, settings, card, tier=3, seed_index=0, tick=0,
                                taken_names={"Mara Quill"})
    assert portrait.name == "Brand New"
    retry = fake.requests[-1]
    assert "already taken" in retry.messages[-1].content
    assert "Mara Quill" in retry.messages[0].content
    assert retry.max_tokens == settings.birth.max_tokens


async def test_persona_cards_markdown_flags_unpleasant(world, sessions, settings):
    from asapville.cli import persona_cards_markdown

    with sessions() as s:
        md = persona_cards_markdown(s, settings)
    assert md.count("\n## ") == 5 and "Unpleasant:" in md and "⚠ unpleasant" in md
