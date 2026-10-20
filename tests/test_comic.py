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
    assert "Глава 1" in page and "Привет!" in page
    assert page.count("<svg") + page.count('class="face"') == 2  # a portrait or an ink bust each
    assert comic.face_svg.__doc__  # the same face every time
    with sessions() as s:
        a = s.scalar(comic.select(comic.Agent))
        assert comic.face_svg(a) == comic.face_svg(a)


async def test_a_portrait_prompt_is_built_from_the_citizens_own_looks(world, sessions):
    with sessions() as s:
        a = s.scalar(comic.select(comic.Agent))
        text = comic.portrait_prompt(a)
    assert "manga" in text and "Hair:" in text and "no text" in text


async def test_panels_get_drawn_art_with_the_cast_described(world, sessions, tmp_path, monkeypatch):
    import io

    from PIL import Image

    from asapville import images

    prompts = []

    def fake_draw(prompt, square=False):
        prompts.append(prompt)
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (200, 30, 30)).save(buf, format="PNG")
        return buf.getvalue()

    monkeypatch.setattr(images, "available", lambda: "fake")
    monkeypatch.setattr(images, "draw", fake_draw)
    with sessions() as s:
        a = s.scalar(comic.select(comic.Agent))
        script = comic.Script(title="T", panels=[comic.Panel(setting="bakery", characters=[a.name],
                                                             scene="He counts the loaves.")])
        art = comic.draw_panels(s, script, tmp_path / "ch")
        page = comic.render_html(s, script, art=art)
    assert "counts the loaves" in prompts[0] and a.name.split()[0] + ":" in prompts[0]
    assert "no text" in prompts[0] and "background:url(data:image/png" in page
    assert Image.open(art[0]).mode == "L"  # ink, not colour
