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

    def fake_draw(prompt, size=(1024, 768), refs=None):
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


async def test_portraits_go_along_as_references_when_the_model_takes_them(world, sessions, tmp_path,
                                                                         monkeypatch):
    import io

    from PIL import Image

    from asapville import images

    calls = []

    def fake_draw(prompt, size=(1024, 768), refs=None):
        calls.append((prompt, size, refs))
        buf = io.BytesIO()
        Image.new("RGB", (64, 48)).save(buf, format="PNG")
        return buf.getvalue()

    monkeypatch.setattr(images, "available", lambda: "cloudflare")
    monkeypatch.setattr(images, "takes_refs", lambda: True)
    monkeypatch.setattr(images, "draw", fake_draw)
    monkeypatch.setattr(comic, "FACES", tmp_path / "faces")
    with sessions() as s:
        a, b = list(s.scalars(comic.select(comic.Agent)))[:2]
        (tmp_path / "faces").mkdir()
        (tmp_path / "faces" / f"{a.id}.png").write_bytes(b"face of a")  # b has no portrait
        script = comic.Script(title="T", panels=[comic.Panel(characters=[a.name, b.name], scene="x."),
                                                 comic.Panel(characters=[b.name], scene="y."),
                                                 comic.Panel(scene="z.")])
        comic.draw_panels(s, script, tmp_path / "ch")
    (p1, size1, refs1), (p2, _, refs2), (_, size3, _) = calls
    assert refs1 == [b"face of a"] and f"image 0 is {a.name.split()[0]}" in p1
    assert refs2 == [] and "Reference pictures" not in p2
    assert size1 == comic.PANEL_SIZE and size3 == comic.WIDE_PANEL_SIZE  # the odd last panel spans the page


def test_each_cloudflare_model_gets_its_own_request(monkeypatch):
    import io
    import json

    from PIL import Image

    from asapville import images

    monkeypatch.delenv("ASAPVILLE_CF_IMAGE_MODEL", raising=False)
    assert images.cf_model() == images.CF_DEFAULT_MODEL
    monkeypatch.setenv("ASAPVILLE_CF_IMAGE_MODEL", "@cf/leonardo/phoenix-1.0")
    assert images.cf_model() == "@cf/leonardo/phoenix-1.0"

    buf = io.BytesIO()
    Image.new("RGB", (900, 900)).save(buf, format="JPEG")
    body, ctype = images.cf_request("@cf/black-forest-labs/flux-2-klein-9b", "a bakery", (1024, 1024),
                                    [buf.getvalue()] * 5)
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="prompt"' in body and b'name="input_image_3"' in body and b"input_image_4" not in body
    assert b'name="steps"' not in body  # klein's steps are fixed
    assert b"\x89PNG" in body  # references are re-encoded small PNGs

    body, ctype = images.cf_request("@cf/leonardo/phoenix-1.0", "a bakery", (1536, 640), [])
    sent = json.loads(body)
    assert ctype == "application/json" and sent["width"] == 1536 and "text" in sent["negative_prompt"]
    sent = json.loads(images.cf_request("@cf/black-forest-labs/flux-1-schnell", "a bakery", (1, 1), [])[0])
    assert set(sent) == {"prompt", "steps"}  # schnell refuses any other field
