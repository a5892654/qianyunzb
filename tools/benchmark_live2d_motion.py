"""Measure the actual selected skin's delivery FPS using local-only fixtures."""
import asyncio
import collections
import json
import sys
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
import config
from main import App, build_web
from live_overlay import Live2DOverlay


async def main():
    app = object.__new__(App)
    app.cfg = config.merge(config.DEFAULTS, json.loads(Path(config.CONFIG_PATH).read_text(encoding="utf-8")))
    app.cfg["live2d"]["enabled"] = True
    app.ws, app.history, app.status = set(), collections.deque(), {}
    app.broadcast = SimpleNamespace()
    runner = web.AppRunner(build_web(app))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    overlay = Live2DOverlay(lambda: {"size": 720, "zoom": 1})
    try:
        await asyncio.to_thread(overlay.start, f"http://127.0.0.1:{port}/live2d?overlay=1", 720)
        await asyncio.sleep(1)
        last, hashes, count = None, set(), 0
        started = time.monotonic()
        while time.monotonic() - started < 4:
            frame = overlay.frame()
            if frame is not None and frame is not last:
                last = frame
                count += 1
                hashes.add(zlib.crc32(frame))
            await asyncio.sleep(0.008)
        result = {**overlay.snapshot(), "observed_fps": round(count / (time.monotonic() - started), 1),
                  "different_images": len(hashes), "error": overlay.error}
        print(json.dumps(result), flush=True)
        if len(sys.argv) > 1:
            Path(sys.argv[1]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    finally:
        for socket in list(app.ws):
            await socket.close()
        await asyncio.to_thread(overlay.stop)
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
