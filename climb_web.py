"""Local climbing controller and browser-source endpoints."""

import asyncio
import json
import time

from aiohttp import WSMsgType, web

from climb import COMMANDS, FOLLOW_EFFECTS, GIFTS
from climb_voice import ClimbPresenter
from climb_weather import WEATHERS


def bind_climb(routes, app, page):
    @routes.get("/climb")
    async def play(_):
        return page("climb.html")

    @routes.get("/api/climb/stream.mjpg")
    async def stream(request):
        response = web.StreamResponse(headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=frame", "Cache-Control": "no-store"})
        await response.prepare(request)
        app.climb.watchers += 1
        try:
            while True:
                started = time.monotonic()
                frame = await asyncio.to_thread(app.climb_scene.jpeg)
                await response.write(f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n"
                                     .encode() + frame + b"\r\n")
                await asyncio.sleep(max(0, 1 / 22 - (time.monotonic() - started)))
        except (ConnectionResetError, RuntimeError):
            pass
        finally:
            app.climb.watchers -= 1
        return response

    @routes.get("/api/climb/state")
    async def state(_):
        game = app.climb.snapshot()
        active = app._is_climb()
        voice = _narration_enabled(app) and app.cfg["tts"].get("enabled", True)
        return web.json_response({
            **{key: game[key] for key in ("auto", "finished", "round", "result_left", "height",
                                          "best", "turns", "zone", "jets", "misses", "slips",
                                          "comebacks", "terrain", "landmark", "weather", "temperature_c")},
            "active": active, "voice": voice,
            "warming_up": active and voice and app._engine_booting,
            "ai_status": getattr(app, "climb_ai_status", "") if active and voice else "",
        })

    @routes.get("/api/climb/control")
    async def control(request):
        if request.headers.get("Origin") != f"http://{request.host}":
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096)
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                    if isinstance(payload, dict):
                        _control(app.climb, payload)
                except (ValueError, OverflowError):
                    continue
            elif message.type == WSMsgType.ERROR:
                break
        return ws


def _control(game, payload):
    action = payload.get("action")
    if not isinstance(action, str):
        return
    if action == "auto" and type(payload.get("enabled")) is bool:
        game.set_auto(payload["enabled"])
    elif action == "reset":
        game.reset()
    elif action == "simulate":
        label, viewer = payload.get("label"), payload.get("viewer")
        if (type(viewer) is int and 0 <= viewer < 20 and isinstance(label, str)
                and (label in COMMANDS or label in GIFTS or label in FOLLOW_EFFECTS)):
            game.simulate(label, viewer)
    elif action == "weather":
        kind, turns = payload.get("kind"), payload.get("turns")
        if isinstance(kind, str) and kind in WEATHERS and (turns is None or
                                                           (type(turns) is int and 1 <= turns <= 60)):
            game.set_weather(kind, turns)


def _narration_enabled(app):
    return app.cfg.get("climb", {}).get("narrate", True) and not app.pipe.paused


def climb_narrating(app):
    return (app._is_climb() and app.climb.auto and _narration_enabled(app)
            and (app.climb.watchers > 0 or app.broadcast.capture.previewing))


async def run_climb(app):
    last = time.monotonic()
    was_voicing = False
    warmup = None
    presenter = ClimbPresenter(app)
    try:
        while True:
            await asyncio.sleep(1 / 60)
            now = time.monotonic()
            if app.climb.watchers or (app._is_climb() and app.broadcast.capture.previewing):
                app.climb.advance(now - last)
            last = now

            active = climb_narrating(app)
            tts = app.cfg["tts"].get("enabled", True)
            voicing = active and tts
            if voicing and not was_voicing and app.engine_idle:
                warmup = asyncio.create_task(app.start_engine_for_live())
            was_voicing = voicing
            ready = not tts or (app.engine_ready.is_set() and app.pipe.tts_ready.is_set())
            if active and ready:
                presenter.tick(now)
            else:
                presenter.suspend()
    finally:
        await presenter.close()
        if warmup is not None:
            warmup.cancel()
            await asyncio.gather(warmup, return_exceptions=True)
