from fastapi.testclient import TestClient
from sqlalchemy import func, select

from asapville.dashboard import snapshot
from asapville.dashboard.app import create_app
from asapville.dashboard.export import DATA_TAG, export_html
from asapville.db.models import AgentTick


async def test_engine_writes_one_snapshot_per_agent_per_tick(world, sessions, settings):
    await world.run(ticks=5, realtime=False)
    with sessions() as s:
        assert s.scalar(select(func.count(AgentTick.id))) == 5 * 5
        frames = snapshot.frames(s, settings)
    assert [f["tick"] for f in frames] == [0, 1, 2, 3, 4]
    assert len(frames[0]["agents"]) == 5
    assert frames[0]["label"].endswith("06:00")
    later = snapshot.frames(s, settings, since_tick=3)
    assert [f["tick"] for f in later] == [4]


async def test_bootstrap_before_first_tick_shows_everyone_at_home(world, sessions, settings):
    with sessions() as s:
        b = snapshot.bootstrap(s, settings)
    assert b["world"]["tick"] == 0 and len(b["agents"]) == 5
    assert len(b["frames"]) == 1
    homes = {a["id"]: a["home_id"] for a in b["agents"]}
    assert all(fa["loc"] == homes[fa["id"]] for fa in b["frames"][0]["agents"])
    assert b["cost"]["calls"] == 5  # the births
    a = b["agents"][0]
    assert a["appearance"]["self_description"] and a["alignment"] and a["model"]


async def test_api_updates_control_and_websocket(world, sessions, settings):
    await world.run(ticks=3, realtime=False)
    app = create_app(settings, sessions, poll_seconds=0.05)
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        assert "asapville-data" in c.get("/").text
        b = c.get("/api/bootstrap").json()
        assert [f["tick"] for f in b["frames"]] == [0, 1, 2]
        u = c.get("/api/updates?since_tick=1&since_event=0").json()
        assert [f["tick"] for f in u["frames"]] == [2]
        assert u["events"] and all(e["visibility"] != "system" for e in u["events"])
        assert c.post("/api/control", json={"action": "pause"}).json()["world"]["paused"] is True
        assert c.post("/api/control", json={"action": "resume"}).json()["world"]["paused"] is False
        assert c.post("/api/control", json={"action": "speed", "value": 3}).json()["world"]["speed"] == 3
        assert c.post("/api/control", json={"action": "explode"}).json()["ok"] is False
        with c.websocket_connect("/ws?since_tick=0&since_event=0") as ws:
            m = ws.receive_json()
            assert m["type"] == "update" and [f["tick"] for f in m["frames"]] == [1, 2]


async def test_export_embeds_the_town(world, sessions, settings):
    await world.run(ticks=2, realtime=False)
    with sessions() as s:
        html = export_html(s, settings)
        frag = export_html(s, settings, fragment=True)
    assert DATA_TAG not in html and '"frames":[{"tick":0' in html
    assert html.startswith("<!doctype html>")
    assert frag.lstrip().startswith("<title>") and "<body>" not in frag and "<!doctype" not in frag
    # the embedded JSON never closes the script tag early
    assert html.count("</script>") == html.count("<script")
