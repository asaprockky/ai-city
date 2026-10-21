"""FastAPI app: the live page, JSON bootstrap, a WebSocket feed and pause/resume/speed.

The feed sends one `update` message whenever the world moves: new frames (one per
finished tick), the events of those ticks, the world clock and the running cost.
When the world runs in this process its EventBus wakes the feed at once; otherwise
(`serve --view-only`, or `run` in another terminal) the database is polled.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from asapville.config import Settings
from asapville.dashboard import snapshot
from asapville.engine import get_world_state
from asapville.world.events import EventBus

log = logging.getLogger("asapville.dashboard")

STATIC = Path(__file__).resolve().parent / "static"
INDEX = STATIC / "index.html"


class Feed:
    """Fan-out of world updates to every open WebSocket."""

    def __init__(self, settings: Settings, sessions: sessionmaker[Session],
                 bus: EventBus | None = None, poll_seconds: float = 1.0) -> None:
        self.settings = settings
        self.sessions = sessions
        self.bus = bus
        self.poll_seconds = poll_seconds
        self.clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self.last_tick = -1
        self.last_event_id = 0
        self._world_sig: tuple[Any, ...] | None = None

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.clients.discard(q)

    def update_since(self, since_tick: int, since_event: int) -> dict[str, Any]:
        with self.sessions() as s:
            ws = get_world_state(s)
            return {
                "type": "update",
                "world": snapshot.world_payload(ws, self.settings),
                "frames": snapshot.frames(s, self.settings, since_tick=since_tick),
                "events": snapshot.events(s, after_id=since_event),
                "cost": snapshot.cost(s),
                "economy": snapshot.economy_payload(s, self.settings),
                "angels": snapshot.angels_payload(s),
                "relations": snapshot.relations_payload(s),
            }

    def _refresh(self) -> dict[str, Any] | None:
        upd = self.update_since(self.last_tick, self.last_event_id)
        w = upd["world"]
        sig = (w["tick"], w["paused"], w["mode"], w["speed"], w["weather"])
        if not upd["frames"] and not upd["events"] and sig == self._world_sig:
            return None
        self._world_sig = sig
        if upd["frames"]:
            self.last_tick = upd["frames"][-1]["tick"]
        if upd["events"]:
            self.last_event_id = upd["events"][-1]["id"]
        return upd

    def broadcast(self, msg: dict[str, Any]) -> None:
        for q in list(self.clients):
            if q.full():
                q.get_nowait()
            q.put_nowait(msg)

    async def run(self) -> None:
        with self.sessions() as s:
            frames = snapshot.frames(s, self.settings)
            self.last_tick = frames[-1]["tick"] if frames else -1
            evs = snapshot.events(s)
            self.last_event_id = evs[-1]["id"] if evs else 0
        wake = self.bus.subscribe() if self.bus else None
        try:
            while True:
                if wake is not None:
                    try:
                        msg = await asyncio.wait_for(wake.get(), timeout=self.poll_seconds)
                        if msg.get("type") != "tick":
                            continue  # events are read from the DB once the tick lands
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(self.poll_seconds)
                try:
                    upd = await asyncio.to_thread(self._refresh)
                except Exception:  # the DB may be mid-migration or locked; try again next round
                    log.exception("dashboard feed refresh failed")
                    continue
                if upd:
                    self.broadcast(upd)
        finally:
            if wake is not None and self.bus is not None:
                self.bus.unsubscribe(wake)


class Control(BaseModel):
    action: str  # pause | resume | speed
    value: float | None = None


def create_app(settings: Settings, sessions: sessionmaker[Session],
               bus: EventBus | None = None, poll_seconds: float = 1.0) -> FastAPI:
    feed = Feed(settings, sessions, bus, poll_seconds)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(feed.run())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title=f"{settings.world.name} dashboard", lifespan=lifespan)
    app.state.feed = feed

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX.read_text(encoding="utf-8")

    @app.get("/api/bootstrap")
    def api_bootstrap() -> dict[str, Any]:
        with sessions() as s:
            return snapshot.bootstrap(s, settings)

    @app.get("/api/updates")
    def api_updates(since_tick: int = -1, since_event: int = 0) -> dict[str, Any]:
        return feed.update_since(since_tick, since_event)

    @app.post("/api/control")
    def api_control(cmd: Control) -> dict[str, Any]:
        with sessions() as s:
            ws = get_world_state(s)
            if cmd.action == "pause":
                ws.paused = True
            elif cmd.action == "resume":
                ws.paused, ws.mode = False, "normal"
            elif cmd.action == "speed" and cmd.value is not None:
                ws.speed = max(0.1, min(50.0, float(cmd.value)))
            else:
                return {"ok": False, "error": f"unknown action {cmd.action!r}"}
            s.commit()
            return {"ok": True, "world": snapshot.world_payload(ws, settings)}

    @app.websocket("/ws")
    async def ws_feed(websocket: WebSocket, since_tick: int = -1, since_event: int = 0) -> None:
        await websocket.accept()
        q = feed.subscribe()
        try:
            catch_up = await asyncio.to_thread(feed.update_since, since_tick, since_event)
            await websocket.send_json(catch_up)
            while True:
                await websocket.send_json(await q.get())
        except WebSocketDisconnect:
            pass
        finally:
            feed.unsubscribe(q)

    return app
