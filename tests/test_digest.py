from asapville import digest


async def test_the_report_is_built_from_the_towns_facts(world, sessions, fake, settings):
    await world.run(ticks=3, realtime=False)
    with sessions() as s:
        text = digest.facts(s, settings)
        fake.script("digest", "День 1, 03:00, весна. В фонде города 100 асапов.")
        report = await digest.write(s, settings, world.gateway)
    assert text.startswith("Day 1, ") and "Town fund" in text
    assert "Shelves: " in text and "Jobs nobody holds: " in text and "Citizens:" in text
    with sessions() as s:
        first = s.scalar(digest.select(digest.Agent))
    assert f"- {first.name}, " in text
    assert report.startswith("День 1")
