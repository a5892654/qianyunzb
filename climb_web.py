"""Local climbing controller and browser-source endpoints."""

import asyncio
import json
import time

from aiohttp import WSMsgType, web

from climb import COMMANDS, GIFTS


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
        return web.json_response({key: game[key] for key in
                                 ("auto", "finished", "round", "result_left", "height", "best",
                                  "turns", "zone", "jets")})

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
        if type(viewer) is int and 0 <= viewer < 20 and isinstance(label, str) and (label in COMMANDS or label in GIFTS):
            game.simulate(label, viewer)


async def run_climb(app):
    last = time.monotonic()
    last_voice = 0.0
    while True:
        await asyncio.sleep(1 / 60)
        now = time.monotonic()
        if app.climb.watchers or (app._is_climb() and app.broadcast.capture.previewing):
            app.climb.advance(now - last)
            voice = app.climb.take_voice()
            if (voice and app._is_climb() and app.cfg["climb"].get("narrate")
                    and not app.pipe.paused and now - last_voice >= 4):
                app.pipe.narrate_fresh(*voice)
                last_voice = now
        last = now
