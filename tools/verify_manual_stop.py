"""手动下播离线回归：使用真实语音队列，模拟音频设备和直播平台。"""
import asyncio
import copy
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import main
from live_schedule import DailyBroadcastSchedule
from pipeline import Pipeline


FAREWELL = "今天播了很久了，下播吃饭饭了。"


class ManualStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = types.SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), dt=None,
                                         pushes=[], auto_stop_task=None)
        self.app.push = lambda kind, data: self.app.pushes.append((kind, data))
        self.app.broadcast = types.SimpleNamespace(running=True, phase="live")

        async def stop():
            self.app.broadcast.running = False
            self.app.broadcast.phase = "idle"
            return {"ok": True}

        self.app.broadcast.stop = AsyncMock(side_effect=stop)
        self.app.broadcast.status = lambda: {"running": self.app.broadcast.running,
                                             "phase": self.app.broadcast.phase}
        self.app.schedule = DailyBroadcastSchedule(self.app)
        self.app.schedule.FAREWELL_TAIL = 0
        self.app.pipe = Mock()
        self.app.pipe.say_job_id.return_value = 12
        self.app.pipe.spoken_done.return_value = True
        self.client = TestClient(TestServer(main.build_web(self.app)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def post(self, path="finish"):
        return await self.client.post("/api/broadcast/" + path, json={}, headers={"X-BVB": "1"})

    async def test_real_pipeline_finishes_audio_before_stop_and_deduplicates(self):
        self.app.pipe = Pipeline(self.app)
        self.app.engine = types.SimpleNamespace(synth=AsyncMock(
            return_value=(np.zeros((100, 2), dtype=np.int16), 1000)))
        started, release = threading.Event(), threading.Event()

        def play(*args):
            started.set()
            if not release.wait(5):
                raise RuntimeError("测试播放未释放")

        self.app.player = types.SimpleNamespace(play_blocking=play, voice_clock=lambda: 0)
        self.app.cfg["tts"].update(enabled=True, stream_enabled=False)
        self.app.pipe.start()
        first = asyncio.create_task(self.post())
        second = None
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 3), self.app.pushes)
            second = asyncio.create_task(self.post())
            status = await (await self.client.get("/api/broadcast/status")).json()
            self.assertTrue(status["finishing"])
            self.app.broadcast.stop.assert_not_awaited()
            self.assertFalse(first.done())
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(first, second), 3)
            for response in responses:
                self.assertTrue((await response.json())["ok"])
            self.app.engine.synth.assert_awaited_once_with(self.app.cfg["tts"], FAREWELL)
            kinds = [kind for kind, _ in self.app.pushes]
            self.assertEqual(kinds.count("speak"), 1)
            self.assertIn("speak_end", kinds)
            self.assertFalse(self.app.broadcast.running)
            self.assertEqual(self.app.schedule.active_action, "")
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
            for task in self.app.pipe.tasks:
                task.cancel()
            await asyncio.gather(*self.app.pipe.tasks, return_exceptions=True)

    async def test_finish_requires_header(self):
        response = await self.client.post("/api/broadcast/finish", json={})
        self.assertEqual(response.status, 403)
        self.app.pipe.say_job_id.assert_not_called()
        self.app.broadcast.stop.assert_not_awaited()

    async def test_idle_does_not_repeat_farewell(self):
        self.app.broadcast.running = False
        self.assertTrue((await (await self.post()).json())["ok"])
        self.app.pipe.say_job_id.assert_not_called()

    async def test_finish_cancels_auto_stop(self):
        timer = asyncio.create_task(asyncio.sleep(3600))
        self.app.auto_stop_task = timer
        self.assertTrue((await (await self.post()).json())["ok"])
        await asyncio.gather(timer, return_exceptions=True)
        self.assertTrue(timer.cancelled())
        self.app.pipe.say_job_id.assert_called_once_with(FAREWELL, "")

    async def test_immediate_stop_still_skips_farewell(self):
        self.assertTrue((await (await self.post("stop")).json())["ok"])
        self.app.pipe.say_job_id.assert_not_called()
        self.app.broadcast.stop.assert_awaited_once()

    async def test_timeout_keeps_live_and_clears_busy_state(self):
        self.app.pipe.spoken_done.return_value = False
        self.app.schedule.FAREWELL_TIMEOUT = 0.01
        body = await (await self.post()).json()
        self.assertFalse(body["ok"])
        self.assertIn("超时", body["error"])
        self.app.broadcast.stop.assert_not_awaited()
        self.assertEqual(self.app.schedule.active_action, "")
        self.assertTrue((await (await self.post("stop")).json())["ok"])

    async def test_queue_rejection_keeps_live(self):
        self.app.pipe.say_job_id.return_value = None
        body = await (await self.post()).json()
        self.assertFalse(body["ok"])
        self.app.broadcast.stop.assert_not_awaited()
        self.assertEqual(self.app.schedule.active_action, "")

    async def test_platform_failure_is_reported(self):
        self.app.broadcast.stop.side_effect = None
        self.app.broadcast.stop.return_value = {"ok": False, "error": "平台停播失败"}
        body = await (await self.post()).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "平台停播失败")
        self.assertEqual(self.app.schedule.active_action, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
