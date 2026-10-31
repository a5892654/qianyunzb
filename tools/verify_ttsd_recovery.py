"""流式语音失败不降级：跳过这一句，后台清残留重启引擎，下一句继续用克隆音。"""
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
    """按脚本逐次产出的假 synth_stream；脚本项是异常就抛。"""

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []

    def __call__(self, text, cfg_tts, timeout_first=25.0):
        self.calls.append(text)
        return self._gen(self.plan.pop(0))

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


def messages(app):
    return [str(c) for c in app.push.call_args_list]


class NoDegradeTests(unittest.IsolatedAsyncioTestCase):
    async def play(self, recorder, recover=None):
        app = FakeApp()
        pipe = Pipeline(app)
        if recover is None:
            pipe._recover_stream_engine = AsyncMock()
        else:
            pipe._recover_stream_engine = recover
        loop = asyncio.get_running_loop()
        item = {"id": 7, "text": "测试一句", "emotion": ""}
        with patch.object(ttsd, "synth_stream", recorder):
            await pipe._play_stream(item, loop)
        await asyncio.sleep(0)  # 让 create_task 跑起来
        return app, pipe

    async def test_failure_skips_line_and_recovers_in_background(self):
        rec = Recorder(["raise"])
        app, pipe = await self.play(rec)
        self.assertEqual(len(rec.calls), 1, "不重试，直接跳过这一句")
        pipe._recover_stream_engine.assert_awaited_once()   # 后台恢复已排上
        app.engine.synth.assert_not_awaited()               # 不降级到整句/SAPI
        self.assertTrue(any("这句跳过（不降级）" in m for m in messages(app)), messages(app))

    async def test_skip_returns_without_waiting_for_engine(self):
        """关键是别让播报队列等 40 秒加载：恢复放进后台任务。"""
        rec = Recorder(["raise"])
        gate = asyncio.Event()

        async def slow_recover():
            await gate.wait()

        app, pipe = await self.play(rec, recover=slow_recover)
        self.assertFalse(gate.is_set(), "恢复仍在进行中时 _play_stream 已返回")
        gate.set()
        await asyncio.sleep(0)  # 收尾，别留悬挂任务

    async def test_mid_stream_break_does_not_replay_words(self):
        rec = Recorder(["raise-after-one"])
        app, pipe = await self.play(rec)
        self.assertEqual(len(rec.calls), 1, "半句已播出，不能整句重放")
        pipe._recover_stream_engine.assert_awaited_once()
        app.engine.synth.assert_not_awaited()

    async def test_success_does_not_trigger_recovery(self):
        rec = Recorder([2])
        app, pipe = await self.play(rec)
        pipe._recover_stream_engine.assert_not_awaited()
        self.assertEqual(len(rec.calls), 1)


class RecoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_recover_restarts_and_reports_failure(self):
        app = FakeApp()
        pipe = Pipeline(app)
        with patch.object(ttsd, "stop", Mock()) as stop, \
                patch.object(ttsd, "ensure", AsyncMock(return_value=False)) as ensure:
            await pipe._recover_stream_engine()
        stop.assert_called_once()
        ensure.assert_awaited_once()
        self.assertTrue(any("没能恢复" in m for m in messages(app)), messages(app))
        self.assertFalse(pipe._recovering)

    async def test_recover_is_not_run_twice_in_parallel(self):
        app = FakeApp()
        pipe = Pipeline(app)
        gate = asyncio.Event()
        calls = []

        async def slow_ensure(cfg, on_status):
            calls.append(1)
            await gate.wait()
            return True

        with patch.object(ttsd, "stop", Mock()), patch.object(ttsd, "ensure", slow_ensure):
            first = asyncio.create_task(pipe._recover_stream_engine())
            await asyncio.sleep(0)
            await pipe._recover_stream_engine()   # 第二次进不去
            gate.set()
            await first
        self.assertEqual(len(calls), 1)

    async def test_recover_swallows_exceptions(self):
        app = FakeApp()
        pipe = Pipeline(app)
        with patch.object(ttsd, "stop", Mock()), \
                patch.object(ttsd, "ensure", AsyncMock(side_effect=OSError("boom"))):
            await pipe._recover_stream_engine()
        self.assertFalse(pipe._recovering)


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

    def test_scanner_crash_never_blocks_start(self):
        """扫描炸了也必须让引擎照常启动：曾经这里抛异常导致全直播间没声。"""
        with patch.object(ttsd, "_ttsd_pids", side_effect=AttributeError("'NoneType' object has no attribute 'splitlines'")):
            stale = ttsd.kill_stale({"ttsd_base": "http://127.0.0.1:8899"})
        self.assertEqual(stale, [])

    def test_kill_failure_does_not_stop_cleanup(self):
        killed = []

        def flaky(pid):
            if pid == 111:
                raise PermissionError("access denied")
            killed.append(pid)

        with patch.object(ttsd, "_listening_pids", return_value={111, 222}), \
                patch.object(ttsd, "_ttsd_pids", return_value=set()), \
                patch.object(ttsd.procutil, "kill_tree", flaky):
            stale = ttsd.kill_stale({"ttsd_base": "http://127.0.0.1:8899"})
        self.assertEqual(killed, [222])
        self.assertEqual(stale, [222])


class DecodeTests(unittest.TestCase):
    """中文 Windows 的 PowerShell 输出不是 UTF-8：按 text=True 解会炸。"""

    def _fake_run(self, stdout):
        return lambda *a, **k: SimpleNamespace(stdout=stdout)

    def test_parses_pids_when_output_is_not_utf8(self):
        out = b"\xbb\xbb\xb2\xe2\xca\xd4\r\n12345\r\n"
        with patch.object(ttsd.subprocess, "run", self._fake_run(out)), \
                patch.object(ttsd.procutil, "CREATE_NO_WINDOW", 0, create=True):
            self.assertEqual(ttsd._ttsd_pids(r"C:\gamedata\ttsd\ttsd.py"), {12345})

    def test_missing_stdout_is_not_a_crash(self):
        with patch.object(ttsd.subprocess, "run", self._fake_run(None)), \
                patch.object(ttsd.procutil, "CREATE_NO_WINDOW", 0, create=True):
            self.assertEqual(ttsd._ttsd_pids(r"C:\gamedata\ttsd\ttsd.py"), set())
            self.assertEqual(ttsd._listening_pids(8899), set())


if __name__ == "__main__":
    unittest.main()
