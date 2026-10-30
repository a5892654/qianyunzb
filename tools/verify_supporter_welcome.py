"""送礼观众回访欢迎：临时数据库、真实消息入口和语音队列，模型/音频离线模拟。"""
import asyncio
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bili import _Handler
from blivedm.models.web import GiftMessage, InteractWordV2Message
from brain import Brain
from config import DEFAULTS, merge
from pipeline import Pipeline
from supporters import GiftSupporters


def gift(**overrides):
    return {"type": "gift", "uid": 123, "name": "小明", "gift": "小花花",
            "num": 1, "paid": True, "value": 1, **overrides}


class SupporterWelcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "supporters.sqlite3"
        cfg = copy.deepcopy(DEFAULTS)
        cfg["ai"]["backend"] = "openai"
        cfg["events"]["enter"].update(merge_window=0, templates="普通欢迎{name}",
                                       supporter_templates="欢迎{name}回来呀！你的心意我一直记着，今天也要开心！")
        cfg["tts"].update(stream_enabled=False)
        self.app = SimpleNamespace(cfg=cfg, pushes=[], dt=None, brain=Brain(), songs=Mock(),
                                   bili=SimpleNamespace(room_id=42, owner_uid=999))
        self.app.push = lambda kind, data: self.app.pushes.append((kind, dict(data)))
        self.app.brain.openai.ask = AsyncMock(return_value="小明来啦，见到你真开心！谢谢你之前的心意，一起开心玩吧！")
        self.app.brain.qoder.ask = AsyncMock(return_value=self.app.brain.openai.ask.return_value)
        self.restart()

    def restart(self):
        self.pipe = self.app.pipe = Pipeline(self.app, supporters=GiftSupporters(self.path))

    def enter(self, **overrides):
        ev = {"type": "enter", "uid": 123, "name": "小明", **overrides}
        self.pipe.on_event(ev)
        return ev

    def remember(self, **overrides):
        self.pipe.on_event(gift(**overrides))
        self.pipe.clear()

    async def reply(self, ev):
        return await self.app.brain.reply(self.app.cfg, self.app.cfg["events"]["enter"], ev)

    async def test_record_survives_restart_and_rename_but_never_matches_by_name(self):
        self.remember()
        self.restart()
        text, source, _ = await self.reply(self.enter(name="改名的小明"))
        self.assertIn("欢迎改名的小明回来", text)
        self.assertEqual(source, "模板")
        self.assertNotIn("普通欢迎", text)
        text, _, _ = await self.reply(self.enter(uid=456, name="改名的小明"))
        self.assertEqual(text, "普通欢迎改名的小明")
        self.app.brain.openai.ask.assert_not_awaited()

    async def test_room_identity_uses_connected_room_and_isolates_history(self):
        self.app.cfg["room_id"] = 84
        self.remember()
        self.assertTrue(GiftSupporters(self.path).contains("42", "123"))
        self.assertFalse(GiftSupporters(self.path).contains(84, 123))
        self.app.bili.room_id = 84
        self.restart()
        self.assertEqual((await self.reply(self.enter()))[0], "普通欢迎小明")
        self.app.bili.room_id = 42
        self.restart()
        self.assertIn("回来", (await self.reply(self.enter()))[0])

    def test_simulated_and_anonymous_gifts_do_not_create_supporters(self):
        for overrides in ({"test": True}, {"uid": 0}, {"num": 0}):
            self.remember(**overrides)
        self.assertFalse(self.pipe.supporters.contains(42, 123))
        self.assertFalse(self.pipe.supporters.contains(42, 0))
        self.assertFalse(self.enter(_gift_supporter=True)["_gift_supporter"])
        self.remember()
        self.assertFalse(self.enter(test=True)["_gift_supporter"])

    def test_no_room_cannot_contaminate_later_room(self):
        self.app.bili.room_id = None
        self.remember()
        self.app.bili.room_id = 42
        self.assertFalse(self.enter()["_gift_supporter"])

    def test_real_free_gift_is_remembered_even_when_thanks_are_disabled(self):
        self.app.cfg["events"]["gift"].update(enabled=False, thank_free=False)
        self.remember(paid=False, value=0)
        self.assertTrue(self.enter()["_gift_supporter"])
        self.app.songs.on_gift.assert_called_once()
        self.assertEqual([job["type"] for job in self.pipe.jobs], ["enter"])

    def test_gifts_during_pause_are_remembered_without_speaking(self):
        self.pipe.paused = True
        self.remember()
        self.assertFalse(self.pipe.jobs)
        self.assertFalse(self.pipe.merging)
        self.pipe.paused = False
        self.assertTrue(self.enter()["_gift_supporter"])

    async def test_mixed_entries_keep_each_supporter_separate_from_ordinary_viewers(self):
        self.remember()
        self.remember(uid=456, name="小红")
        self.app.cfg["events"]["enter"].update(merge_window=4, max_names=1)
        for overrides in ({"uid": 789, "name": "路人甲"}, {},
                          {"uid": 456, "name": "小红"}, {"uid": 888, "name": "路人乙"}):
            self.enter(**overrides)
        self.assertEqual(len(self.pipe.merging), 3)
        for key, pending in list(self.pipe.merging.items()):
            self.pipe._flush_merge(key, pending)
        events = [job["ev"] for job in self.pipe.jobs]
        supporters = [ev for ev in events if ev["_gift_supporter"]]
        ordinary = [ev for ev in events if not ev["_gift_supporter"]]
        self.assertEqual([ev["name"] for ev in supporters], ["小明", "小红"])
        self.assertEqual(len(ordinary), 1)
        self.assertIn("等2位朋友", ordinary[0]["name"])
        for ev in supporters:
            text, _, _ = await self.reply(ev)
            self.assertIn(ev["name"], text)
            self.assertNotIn("路人", text)

    async def test_ai_uses_special_prompt_both_backends_and_preserves_emotion(self):
        self.remember()
        ev = self.enter()
        self.app.cfg["events"]["enter"]["mode"] = "ai"
        self.app.cfg["live2d"]["enabled"] = True
        original = copy.deepcopy(self.app.cfg)
        for backend in ("openai", "qoder"):
            self.app.cfg["ai"]["backend"] = backend
            model = getattr(self.app.brain, backend).ask
            model.return_value = "【开心】小明来啦，见到你真开心！你的心意我记着，一起玩吧！"
            text, source, emotion = await self.reply(ev)
            self.assertEqual((source, emotion), ("AI", "开心"))
            self.assertEqual(text.count("小明"), 1)
            prompt = model.await_args.args[2]
            for expected in ("以前在本直播间送过礼物", "观众小明", "不要播报消费金额", "不要编造"):
                self.assertIn(expected, prompt)
        self.assertEqual(self.app.cfg["events"], original["events"])
        self.assertEqual(self.app.cfg["ai"]["max_chars"], original["ai"]["max_chars"])

    def test_repeat_entry_with_changed_name_in_one_window_only_mentions_one_name(self):
        self.remember()
        self.app.cfg["events"]["enter"].update(merge_window=4, cooldown_user=0)
        self.enter()
        self.enter(name="新名字")
        self.assertEqual(len(self.pipe.merging), 1)
        key, pending = self.pipe.merging.popitem()
        self.pipe._flush_merge(key, pending)
        self.assertEqual(self.pipe.jobs[0]["ev"]["name"], "新名字")

    async def test_ai_empty_or_failure_uses_warm_fallback(self):
        self.remember()
        self.app.cfg["events"]["enter"]["mode"] = "ai"
        ev = self.enter()
        for result in ("", RuntimeError("模型离线")):
            self.app.brain.openai.ask.return_value = result if isinstance(result, str) else ""
            self.app.brain.openai.ask.side_effect = result if isinstance(result, Exception) else None
            text, source, emotion = await self.reply(ev)
            self.assertEqual(text, "欢迎小明回来呀！你的心意我一直记着，今天也要开心！")
            self.assertIn("模板", source)
            self.assertEqual(emotion, "")

    async def test_custom_supporter_text_and_disabling_preserve_ordinary_rules(self):
        self.remember()
        settings = self.app.cfg["events"]["enter"]
        settings.update(supporter_templates="{name}回来一起玩呀！", supporter_ai_prompt="亲切欢迎{name}回访")
        ev = self.enter()
        self.assertEqual((await self.reply(ev))[0], "小明回来一起玩呀！")
        settings["mode"] = "ai"
        await self.reply(ev)
        self.assertEqual(self.app.brain.openai.ask.await_args.args[2], "亲切欢迎小明回访")
        settings.update(supporter_welcome=False, mode="template")
        self.assertEqual((await self.reply(ev))[0], "普通欢迎小明")
        self.restart()
        self.assertFalse(self.enter()["_gift_supporter"])

    def test_supporter_entries_still_respect_filters_pause_and_cooldown(self):
        self.remember()
        self.enter()
        self.enter()
        self.assertEqual(self.app.pushes[-1][1]["skip"], "冷却中")
        for settings, reason in (({"ignore_users": "123"}, "忽略名单"), ({"blocklist": "小明"}, "屏蔽词")):
            self.restart()
            self.app.cfg["filter"].update(settings)
            self.enter()
            self.assertEqual(self.app.pushes[-1][1]["skip"], reason)
            self.assertFalse(self.pipe.jobs)
            self.app.cfg["filter"].update(ignore_users="", blocklist="")
        self.restart()
        self.pipe.paused = True
        self.enter()
        self.assertEqual(self.app.pushes[-1][1]["skip"], "已暂停")
        self.pipe.paused = False
        self.app.cfg["events"]["enter"]["enabled"] = False
        self.enter()
        self.assertEqual(self.app.pushes[-1][1]["skip"], "该类型未启用")
        self.assertFalse(self.pipe.jobs)

    def test_storage_failure_keeps_in_memory_welcome_available(self):
        supporters = GiftSupporters(self.path.parent)  # 目录不能作为 SQLite 文件打开。
        with self.assertLogs("supporters", level="WARNING"):
            supporters.record(42, gift())
        self.assertTrue(supporters.contains(42, 123))
        with self.assertLogs("supporters", level="WARNING"):
            self.assertFalse(supporters.contains(84, 123))

    def test_existing_settings_receive_defaults_without_overwriting_custom_text(self):
        cfg = merge(DEFAULTS, {"events": {"enter": {"templates": "原有欢迎{name}"}}})
        self.assertTrue(cfg["events"]["enter"]["supporter_welcome"])
        self.assertEqual(cfg["events"]["enter"]["templates"], "原有欢迎{name}")
        cfg = merge(DEFAULTS, {"events": {"enter": {"supporter_welcome": False}}})
        self.assertFalse(cfg["events"]["enter"]["supporter_welcome"])

    async def test_real_gift_then_restarted_entry_reaches_synthesis_and_playback(self):
        # 连接刚初始化时，界面的房间缓存可能尚未更新；以实际消息来源为准。
        self.app.bili.room_id = 84
        client = SimpleNamespace(room_id=42)
        _Handler(self.pipe.on_event)._on_gift(client,
            GiftMessage(uid=123, uname="小明", gift_name="小花花", num=1, total_coin=1000, coin_type="gold"))
        self.assertTrue(GiftSupporters(self.path).contains(42, 123))
        self.assertFalse(GiftSupporters(self.path).contains(84, 123))
        self.restart()
        self.app.cfg["events"]["enter"]["mode"] = "ai"
        self.app.engine = SimpleNamespace(synth=AsyncMock(
            return_value=(np.zeros((240, 1), dtype=np.float32), 24000)))
        self.app.player = Mock()
        self.app.player.voice_clock.return_value = 0
        _Handler(self.pipe.on_event)._on_interact_word_v2(client,
            InteractWordV2Message(msg_type=1, uid=123, username="小明"))
        welcome_id = self.pipe.jobs[0]["id"]
        tasks = [asyncio.create_task(worker()) for worker in
                 (self.pipe._gen_worker, self.pipe._synth_worker, self.pipe._play_worker)]
        try:
            async with asyncio.timeout(2):
                while not self.pipe.spoken_done(welcome_id):
                    await asyncio.sleep(0.005)
            spoken = [data["text"] for kind, data in self.app.pushes if kind == "speak"]
            self.assertEqual(spoken, [self.app.brain.openai.ask.return_value])
            self.app.engine.synth.assert_awaited_once_with(self.app.cfg["tts"], spoken[0])
            self.app.player.play_blocking.assert_called_once()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
