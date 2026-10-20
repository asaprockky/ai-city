import json

from asapville import comic


async def test_a_manga_page_from_the_towns_events(world, sessions, fake):
    await world.run(ticks=3, realtime=False)
    with sessions() as s:
        names = [a.name for a in s.scalars(comic.select(comic.Agent))][:2]
        fake.script("comic", json.dumps({"title": "Глава 1", "panels": [
            {"caption": "Утро.", "setting": "площадь", "characters": names,
             "bubbles": [{"who": names[0], "text": "Привет!"}], "sfx": "бах"}]}))
        assert "] " in comic.digest(s, 0, 3) or comic.digest(s, 0, 3) == ""
        script = await comic.write_script(s, world.gateway, "m", 0, 3)
        page = comic.render_html(s, script, chapter="· ДЕНЬ 1")
    assert "Глава 1" in page and "Привет!" in page and page.count("<svg") == 2
    assert comic.face_svg.__doc__  # the same face every time
    with sessions() as s:
        a = s.scalar(comic.select(comic.Agent))
        assert comic.face_svg(a) == comic.face_svg(a)
