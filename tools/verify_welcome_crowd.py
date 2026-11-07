"""人多时通用欢迎、长名字不念、TTS 失控截断的离线验证。"""
import asyncio
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain import Brain
from config import DEFAULTS
from pipeline import Pipeline, speech_cap
from supporters import GiftSupporters


class WelcomeCrowdTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "supporters.sqlite3"
        cfg = copy.deepcopy(DEFAULTS)
        cfg["events"]["enter"].update(
            merge_window=4, crowd_count=3, crowd_window=15, cooldown_user=0,
            templates="普通欢迎{name}", crowd_templates="大家好呀，欢迎新朋友！",
            supporter_templates="欢迎{name}回来！")
        cfg["filter"]["name_read_max"] = 8
        cfg["ai"]["backend"] = "openai"
        self.cfg = cfg
        self.app = SimpleNamespace(cfg=cfg, pushes=[], dt=None, brain=Brain(), songs=Mock(),
                                   bili=SimpleNamespace(room_id=42, owner_uid=999))
        self.app.push = lambda kind, data: self.app.pushes.append((kind, dict(data)))
        self.pipe = Pipeline(self.app, supporters=GiftSupporters(self.path))

    def enter(self, uid, name, **overrides):
        ev = {"type": "enter", "uid": uid, "name": name, **overrides}
        self.pipe.on_event(ev)
        return ev

    def flush_enters(self):
        events = []
        for key, pending in list(self.pipe.merging.items()):
            self.pipe.merging.pop(key)
            self.pipe._flush_merge(key, pending)
        events.extend(job["ev"] for job in self.pipe.jobs if job["type"] == "enter")
        return events

    async def reply(self, ev):
        return await self.app.brain.reply(self.cfg, self.cfg["events"]["enter"], ev)

    async def test_lone_entrant_keeps_name(self):
        self.enter(1, "小明")
        ev, = self.flush_enters()
        self.assertFalse(ev["_crowd"])
        self.assertEqual((await self.reply(ev))[0], "普通欢迎小明")

    async def test_three_quick_entries_switch_to_nameless_crowd_welcome(self):
        for uid, name in ((1, "小明"), (2, "小红"), (3, "小刚")):
            self.enter(uid, name)
        ev, = self.flush_enters()
        self.assertTrue(ev["_crowd"])
        text, source, _ = await self.reply(ev)
        self.assertEqual((text, source), ("大家好呀，欢迎新朋友！", "模板"))
        for name in ("小明", "小红", "小刚"):
            self.assertNotIn(name, text)

    async def test_crowd_ai_prompt_forbids_names(self):
        for uid, name in ((1, "小明"), (2, "小红"), (3, "小刚")):
            self.enter(uid, name)
        ev, = self.flush_enters()
        self.cfg["events"]["enter"]["mode"] = "ai"
        self.app.brain.openai.ask = AsyncMock(return_value="欢迎新来的朋友们，随便坐！")
        text, source, _ = await self.reply(ev)
        prompt = self.app.brain.openai.ask.await_args.args[2]
        self.assertEqual((text, source), ("欢迎新来的朋友们，随便坐！", "AI"))
        self.assertIn("不要念任何观众名字", prompt)
        for name in ("小明", "小红", "小刚"):
            self.assertNotIn(name, prompt)

    async def test_hybrid_roll_ai_returns_whole_ai_sentence(self):
        self.cfg["events"]["enter"]["mode"] = "hybrid"
        self.app.brain.openai.ask = AsyncMock(return_value="今天也一起玩点开心的吧")
        self.enter(1, "小明")
        ev, = self.flush_enters()
        with patch("brain.random.random", return_value=0.0):
            text, source, _ = await self.reply(ev)
        self.assertEqual((text, source), ("今天也一起玩点开心的吧", "AI"))
        prompt = self.app.brain.openai.ask.await_args.args[2]
        self.assertNotIn("本次欢迎的开头已经由程序固定", prompt)

    async def test_hybrid_roll_preset_returns_whole_preset_sentence(self):
        self.cfg["events"]["enter"]["mode"] = "hybrid"
        self.app.brain.openai.ask = AsyncMock(return_value="不该出现的句子")
        self.enter(1, "小明")
        ev, = self.flush_enters()
        with patch("brain.random.random", return_value=0.9):
            text, source, _ = await self.reply(ev)
        self.assertEqual((text, source), ("普通欢迎小明", "预制"))
        self.app.brain.openai.ask.assert_not_awaited()

    async def test_hybrid_ai_failure_keeps_preset_only(self):
        self.cfg["events"]["enter"]["mode"] = "hybrid"
        self.app.brain.openai.ask = AsyncMock(side_effect=RuntimeError("模型离线"))
        self.enter(1, "小明")
        ev, = self.flush_enters()
        with patch("brain.random.random", return_value=0.0):
            text, source, _ = await self.reply(ev)
        self.assertEqual(text, "普通欢迎小明")
        self.assertIn("模板", source)

    async def test_hybrid_crowd_ai_roll_has_no_names(self):
        self.cfg["events"]["enter"]["mode"] = "hybrid"
        self.app.brain.openai.ask = AsyncMock(return_value="今天也一起热闹热闹")
        for uid, name in ((1, "小明"), (2, "小红"), (3, "小刚")):
            self.enter(uid, name)
        ev, = self.flush_enters()
        with patch("brain.random.random", return_value=0.0):
            text, source, _ = await self.reply(ev)
        self.assertEqual((text, source), ("今天也一起热闹热闹", "AI"))
        prompt = self.app.brain.openai.ask.await_args.args[2]
        self.assertIn("不要念任何观众名字", prompt)
        for name in ("小明", "小红", "小刚"):
            self.assertNotIn(name, prompt)

    async def test_hybrid_crowd_preset_roll_is_generic(self):
        self.cfg["events"]["enter"]["mode"] = "hybrid"
        self.app.brain.openai.ask = AsyncMock(return_value="不该出现的句子")
        for uid, name in ((1, "小明"), (2, "小红"), (3, "小刚")):
            self.enter(uid, name)
        ev, = self.flush_enters()
        with patch("brain.random.random", return_value=0.9):
            text, source, _ = await self.reply(ev)
        self.assertEqual((text, source), ("大家好呀，欢迎新朋友！", "预制"))
        self.app.brain.openai.ask.assert_not_awaited()

    async def test_hybrid_long_name_preset_uses_new_friend(self):
        self.cfg["events"]["enter"]["mode"] = "hybrid"
        self.app.brain.openai.ask = AsyncMock(return_value="今天也一起玩吧")
        self.enter(1, "这是一个非常非常长的名字")
        ev, = self.flush_enters()
        with patch("brain.random.random", return_value=0.9):
            text, source, _ = await self.reply(ev)
        self.assertEqual((text, source), ("普通欢迎新朋友", "预制"))

    async def test_long_name_becomes_new_friend(self):
        self.enter(1, "这是一个非常非常长的名字")
        ev, = self.flush_enters()
        self.assertFalse(ev["_crowd"])
        self.assertEqual(ev["raw_name"], "这是一个非常非常长的名字")
        self.assertEqual((await self.reply(ev))[0], "普通欢迎新朋友")

    async def test_long_name_supporter_becomes_old_friend(self):
        self.pipe.on_event({"type": "gift", "uid": 7, "name": "老观众", "gift": "小花花",
                            "num": 1, "paid": True, "value": 1})
        self.pipe.clear()
        self.enter(7, "这是一个非常非常长的名字")
        ev, = self.flush_enters()
        self.assertTrue(ev["_gift_supporter"])
        self.assertFalse(ev["_crowd"])
        self.assertEqual((await self.reply(ev))[0], "欢迎老朋友回来！")

    async def test_crowd_does_not_downgrade_supporter_welcome(self):
        self.pipe.on_event({"type": "gift", "uid": 7, "name": "老观众", "gift": "小花花",
                            "num": 1, "paid": True, "value": 1})
        self.pipe.clear()
        self.enter(1, "小明")
        self.enter(2, "小红")
        self.enter(7, "老观众")
        events = self.flush_enters()
        supporter = next(ev for ev in events if ev["_gift_supporter"])
        ordinary = next(ev for ev in events if not ev["_gift_supporter"])
        self.assertFalse(supporter["_crowd"])
        self.assertTrue(ordinary["_crowd"])
        self.assertEqual((await self.reply(supporter))[0], "欢迎老观众回来！")

    async def test_test_events_do_not_trigger_crowd(self):
        self.cfg["events"]["enter"]["crowd_window"] = 0
        for uid in (1, 2, 3):
            self.enter(uid, f"观众{uid}", test=True)
        self.assertEqual(len(self.pipe._enter_times), 0)

    def test_speech_cap_has_headroom_but_bounds_runaway(self):
        normal = "lzm1230来啦，正好赶上本鱼的营业时间！"
        self.assertGreaterEqual(speech_cap(normal), 8)
        self.assertLess(speech_cap(normal), 20)
        self.assertGreater(speech_cap("长" * 200), speech_cap(normal))
        self.assertLess(speech_cap(normal), 163.04)

    async def test_runaway_stream_is_cut_and_closed(self):
        from dtgame import ttsd
        text = "lzm1230来啦，正好赶上本鱼的营业时间！"

        class FakePlayer:
            def __init__(self):
                self.fed = 0
                self.skipped = False
                self.ended = False

            def begin_stream(self, *args, **kwargs):
                pass

            def feed_stream(self, pcm, sr):
                self.fed += len(pcm) / sr

            def end_stream(self):
                self.ended = True

            def skip(self):
                self.skipped = True

            def stream_stats(self):
                return {"gaps": 0, "gap_ms": 0, "output_underflows": 0}

        closed = False
        chunk = np.zeros((24000, 1), dtype=np.int16)

        async def fake_stream(_text, _cfg, timeout_first=25.0):
            nonlocal closed
            try:
                for _ in range(200):
                    yield chunk, 24000
            finally:
                closed = True

        original = ttsd.synth_stream
        ttsd.synth_stream = fake_stream
        try:
            self.app.player = FakePlayer()
            item = {"id": 1, "type": "manual", "text": text, "emotion": ""}
            await self.pipe._play_stream(item, asyncio.get_running_loop())
        finally:
            ttsd.synth_stream = original

        self.assertTrue(closed)
        self.assertTrue(self.app.player.skipped)
        self.assertTrue(self.app.player.ended)
        self.assertLessEqual(self.app.player.fed, speech_cap(text))
        warnings = [data["msg"] for kind, data in self.app.pushes
                    if kind == "log" and "截断" in data.get("msg", "")]
        self.assertEqual(len(warnings), 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
