"""流式语音失败不再降级：清残留 -> 重启引擎 -> 重试一次，仍失败就跳过这一句。"""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from dtgame import ttsd
from pipeline import Pipeline
from tts import Player


def chunk(frames=2400, sr=1000):
    return np.full((frames, 1), 8192, np.int16), sr


class Recorder:
    """按脚本逐次产出的假 synth_stream；第 N 次是异常就抛。"""

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []

    def __call__(self, text, cfg_tts, timeout_first=25.0):
        self.calls.append(text)
        step = self.plan.pop(0)
        return self._gen(step)

    async def _gen(self, step):
        if step == "raise":
            raise ConnectionRefusedError("connect 8899 refused")
        if step == "raise-after-one":
            yield chunk()
            raise ConnectionResetError("stream dropped")
        for _ in range(step):
            yield chunk()


class FakeApp:
    def __init__(self):
        self.cfg = {"audio": {}, "tts": {"stream_buffer_seconds": 0.0}}
        self.player = Player()
        self.player.sr = 1000
        self.player.ensure = Mock()
        self.player._wait_done = Mock()
        self.push = Mock()
        self.engine = SimpleNamespace(synth=AsyncMock(return_value=(np.zeros((10, 1), np.int16), 24000)))


class NoDegradeTests(unittest.IsolatedAsyncioTestCase):
    async def play(self, recorder, ensure_ok=True):
        app = FakeApp()
        pipe = Pipeline(app)
        loop = asyncio.get_running_loop()
        item = {"id": 7, "text": "测试一句", "emotion": ""}
        with patch.object(ttsd, "synth_stream", recorder), \
                patch.object(ttsd, "ensure", AsyncMock(return_value=ensure_ok)) as ensure, \
                patch.object(ttsd, "stop", Mock()):
            await pipe._play_stream(item, loop)
        return app, pipe, ensure

    async def test_failure_restarts_and_retries_without_degrading(self):
        rec = Recorder(["raise", 3])
        app, _, ensure = await self.play(rec, ensure_ok=True)
        self.assertEqual(len(rec.calls), 2, "应当重试一次")
        ensure.assert_awaited_once()
        app.engine.synth.assert_not_awaited()          # 不降级到整句/SAPI
        self.assertGreaterEqual(app.push.call_count, 2)

    async def test_second_failure_skips_line(self):
        rec = Recorder(["raise", "raise"])
        app, _, _ = await self.play(rec)
        self.assertEqual(len(rec.calls), 2, "只重试一次，不无限循环")
        app.engine.synth.assert_not_awaited()
        msgs = [str(c) for c in app.push.call_args_list]
        self.assertTrue(any("已跳过（不降级）" in m for m in msgs), msgs)

    async def test_engine_cannot_restart_skips_without_retry(self):
        rec = Recorder(["raise"])
        app, _, ensure = await self.play(rec, ensure_ok=False)
        self.assertEqual(len(rec.calls), 1)
        ensure.assert_awaited_once()
        app.engine.synth.assert_not_awaited()
        msgs = [str(c) for c in app.push.call_args_list]
        self.assertTrue(any("没能恢复" in m for m in msgs), msgs)

    async def test_mid_stream_break_does_not_replay_words(self):
        rec = Recorder(["raise-after-one"])
        app, pipe, _ = await self.play(rec)
        self.assertEqual(len(rec.calls), 1, "半句已播出，不能整句重放")
        app.engine.synth.assert_not_awaited()


class KillStaleTests(unittest.TestCase):
    def test_kills_only_foreign_ttsd_processes(self):
        killed = []
        with patch.object(ttsd, "_listening_pids", return_value={111, 222}), \
                patch.object(ttsd, "_ttsd_pids", return_value={222, 333}), \
                patch.object(ttsd.procutil, "kill_tree", lambda pid: killed.append(pid)):
            stale = ttsd.kill_stale({"ttsd_script": r"C:\gamedata\ttsd\ttsd.py",
                                     "ttsd_base": "http://127.0.0.1:8899"})
        self.assertEqual(sorted(stale), [111, 222, 333])
        self.assertEqual(killed, [111, 222, 333])

    def test_skips_our_own_process(self):
        import os
        killed = []
        with patch.object(ttsd, "_listening_pids", return_value={os.getpid()}), \
                patch.object(ttsd, "_ttsd_pids", return_value=set()), \
                patch.object(ttsd.procutil, "kill_tree", lambda pid: killed.append(pid)):
            stale = ttsd.kill_stale({"ttsd_base": "http://127.0.0.1:8899"})
        self.assertEqual(stale, [])
        self.assertEqual(killed, [])


if __name__ == "__main__":
    unittest.main()
