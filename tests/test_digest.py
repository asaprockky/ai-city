import json

from asapville import digest


async def test_the_paper_is_built_from_the_towns_facts_and_words(world, sessions, fake, settings):
    await world.run(ticks=3, realtime=False)
    with sessions() as s:
        first = s.scalar(digest.select(digest.Agent))
        text = digest.facts(s, settings)
        fake.script("digest", json.dumps({
            "headline": "Хлеб гниёт, рынок пуст", "lede": "Главное.",
            "stories": [{"title": "Сделка", "body": "Было так.", "people": [first.name],
                         "quote": {"who": first.name, "text": "Беру!"}}],
            "citizens": [{"name": first.name, "line": "печёт"}], "next": "Выборы."}))
        paper = await digest.write(s, settings, world.gateway)
        post = digest.as_text(s, settings, paper)
        page = digest.render_html(s, settings, paper)
    assert text.startswith("Day 1, ") and "Shelves: " in text and f"- {first.name}, " in text
    assert post.startswith("🗞 Хлеб гниёт") and "«Беру!»" in post and f"• {first.name}: печёт" in post
    assert "<h1>Хлеб гниёт, рынок пуст</h1>" in page and "<blockquote>" in page and "<svg" in page


async def test_big_moments_are_picked_for_breaking_news(world, sessions):
    from asapville.world.events import emit

    with sessions() as s:
        a = s.scalar(digest.select(digest.Agent))
        emit(s, tick=1, kind="notice", visibility="public", summary="chatter",
             data={"text": "chatter", "author": a.name})
        emit(s, tick=1, kind="notice", visibility="public", summary="The town elected its council",
             data={"text": "x", "author": "the town clerk"})
        emit(s, tick=1, kind="property", actor=a, visibility="public", summary=f"{a.name} bought the Mill")
        s.commit()
        big = [e.summary for e in digest.breaking(s, 0) if digest.is_breaking(e)]
    assert big == ["The town elected its council", f"{a.name} bought the Mill"]


async def test_a_bulletin_of_the_hours_with_a_builders_note(world, sessions, fake, settings):
    await world.run(ticks=3, realtime=False)
    with sessions() as s:
        fake.script("chunk", "Часы 0–2 (день 1).\n\n• **Хлеб**. Рут печёт.")
        text = await digest.write_chunk(s, settings, world.gateway, hours=3, note="Ничего не сломалось.")
    assert text.startswith("Часы 0–2") and "**" not in text and text.endswith("🛠 Ничего не сломалось.")
