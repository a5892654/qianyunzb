"""离线验证弹幕与游戏上下文、播报优先级；不连接 B站、模型或音频设备。"""
import asyncio
import copy
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bili import _Handler
from blivedm.models.web import DanmakuMessage
from brain import Brain
from config import DEFAULTS
from dtgame.player import Player
from pipeline import Pipeline


def make_app():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["ai"]["backend"] = "openai"
    cfg["live2d"]["enabled"] = False
    app = SimpleNamespace(cfg=cfg, pushes=[], dt=None, brain=Brain(),
                          bili=SimpleNamespace(owner_uid=999),
                          songs=SimpleNamespace(on_danmaku=lambda ev: False))
    app.push = lambda kind, data: app.pushes.append((kind, dict(data)))
    app.pipe = Pipeline(app)
    app.brain.openai.ask = AsyncMock(return_value="先把血回上来，多谢提醒！")
    return app


def game_state():
    return {"screen": "battle", "run": {"character": "奈尔", "hp": 12, "max_hp": 60},
            "battle": {"hp": 12, "max_hp": 60, "energy": 2, "max_energy": 3}}


def attach_game(app):
    bridge = Mock()
    bridge.state.return_value = game_state()
    bridge.screen.return_value = {"screen": "battle"}
    bridge.end_turn.return_value = {"ok": True}
    player = Player({}, bridge, speak=app.pipe.narrate, pipe=app.pipe)
    player.running = True
    app.dt = player
    return player


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def worker(self, app):
        task = asyncio.create_task(app.pipe._gen_worker())
        self.addAsyncCleanup(self.stop_worker, task)
        return task

    @staticmethod
    async def stop_worker(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def take(self, app):
        return await asyncio.wait_for(app.pipe.synth_q.get(), 1)

    def danmaku(self, app, text="记得回血", uid=123):
        _Handler(app.pipe.on_event)._on_danmaku(
            None, DanmakuMessage(uid=uid, uname="小明", msg=text))

    async def test_real_event_replies_before_queued_narration_and_shares_context(self):
        app = make_app()
        player = attach_game(app)
        # 通过真实 step 更新缓存，弹幕回复不能另外阻塞查询游戏桥。
        player._ask = AsyncMock(return_value={"action": "end", "say": "这回合有点险"})
        await player.step()
        self.danmaku(app)
        await self.worker(app)
        first = await self.take(app)
        second = await self.take(app)
        self.assertEqual([first["type"], second["type"]], ["danmaku", "narration"])
        self.assertEqual(first["text"], "先把血回上来，多谢提醒！")
        prompt = app.brain.openai.ask.call_args.args[2]
        for expected in ("记得回血", "奈尔", "12/60", "这回合有点险"):
            self.assertIn(expected, prompt)
        player.bridge.state.assert_called_once()
        context = player._chat_context()
        self.assertIn("小明", context)
        self.assertIn("记得回血", context)
        self.assertIn(first["text"], context)

    async def test_decision_request_includes_recent_interaction(self):
        app = make_app()
        player = attach_game(app)
        player.cfg = {"openai_base": "http://unused/v1", "openai_model": "test"}
        player.remember_interaction({"name": "小明", "text": "记得回血"}, "马上喝一口")
        response = AsyncMock()
        response.status = 200
        response.text.return_value = '{"choices":[{"message":{"content":"{\\"action\\":\\"end\\"}"}}]}'
        session = AsyncMock()
        session.post = Mock()
        request = AsyncMock()
        request.__aenter__.return_value = response
        session.post.return_value = request
        client = AsyncMock()
        client.__aenter__.return_value = session
        with patch("dtgame.player.aiohttp.ClientSession", return_value=client):
            await player._ask("当前战斗状态")
        messages = session.post.call_args.kwargs["json"]["messages"]
        self.assertIn("记得回血", messages[-2]["content"])
        self.assertIn("马上喝一口", messages[-2]["content"])
        self.assertIn("不要重复播报", messages[-2]["content"])
        self.assertEqual(messages[-1]["content"], "当前战斗状态")

    async def test_manual_announcement_keeps_priority(self):
        app = make_app()
        attach_game(app)
        app.pipe.narrate("继续打牌")
        self.danmaku(app)
        app.pipe.say("主播手动通知")
        await self.worker(app)
        types = [(await self.take(app))["type"] for _ in range(3)]
        self.assertEqual(types, ["manual", "danmaku", "narration"])

    async def test_reply_reaches_synthesis_and_playback_before_narration(self):
        app = make_app()
        attach_game(app)
        app.cfg["tts"].update(qwen_enabled=False)
        pcm = np.zeros((240, 1), dtype=np.float32)
        app.engine = SimpleNamespace(synth=AsyncMock(return_value=(pcm, 24000)))
        app.player = Mock()
        app.player.voice_clock.return_value = 0
        done = asyncio.Event()
        played = []
        push = app.push

        def record(kind, data):
            push(kind, data)
            if kind == "speak":
                played.append(data["text"])
            if kind == "speak_end" and len(played) == 2:
                done.set()

        app.push = record
        app.pipe.narrate("继续打牌")
        self.danmaku(app)
        await self.worker(app)
        for worker in (app.pipe._synth_worker, app.pipe._play_worker):
            task = asyncio.create_task(worker())
            self.addAsyncCleanup(self.stop_worker, task)
        await asyncio.wait_for(done.wait(), 2)
        self.assertEqual(played, ["先把血回上来，多谢提醒！", "继续打牌"])
        self.assertEqual([c.args[1] for c in app.engine.synth.await_args_list], played)
        self.assertEqual(app.player.play_blocking.call_count, 2)

    async def test_queue_eviction_and_clear_release_game_wait(self):
        app = make_app()
        player = attach_game(app)
        app.cfg["filter"]["max_queue"] = 1
        job = app.pipe.narrate("继续打牌")
        self.assertFalse(app.pipe.spoken_done(job))
        self.danmaku(app)
        self.assertTrue(app.pipe.spoken_done(job))
        await asyncio.wait_for(player._wait_spoken(job), 0.1)
        self.assertEqual([j["type"] for j in app.pipe.jobs], ["danmaku"])
        app.pipe.clear()
        job = app.pipe.narrate("下一句")
        app.pipe.clear()
        self.assertTrue(app.pipe.spoken_done(job))

    async def test_expired_narration_releases_game_wait(self):
        app = make_app()
        job = app.pipe.narrate("过期解说")
        app.pipe.jobs[0]["ts"] -= 100
        await self.worker(app)
        await asyncio.sleep(0)
        self.assertTrue(app.pipe.spoken_done(job))
        self.assertTrue(app.pipe.synth_q.empty())

    async def test_filters_do_not_leak_rejected_chat_to_game(self):
        app = make_app()
        player = attach_game(app)
        self.danmaku(app, uid=999)
        self.assertEqual(app.pushes[-1][1]["skip"], "主播本人")
        app.pipe.paused = True
        self.danmaku(app)
        self.assertEqual(app.pushes[-1][1]["skip"], "已暂停")
        app.pipe.paused = False
        app.cfg["events"]["danmaku"]["enabled"] = False
        self.danmaku(app)
        self.assertEqual(app.pushes[-1][1]["skip"], "该类型未启用")
        self.assertFalse(app.pipe.jobs)
        self.assertFalse(player.live_chat)
        app.brain.openai.ask.assert_not_called()

    async def test_chat_still_works_without_running_game(self):
        app = make_app()
        self.danmaku(app)
        await self.worker(app)
        self.assertEqual((await self.take(app))["type"], "danmaku")
        self.assertNotIn("当前直播游戏上下文", app.brain.openai.ask.call_args.args[2])

    async def test_fixed_reply_is_also_shared_without_model_call(self):
        app = make_app()
        player = attach_game(app)
        app.cfg["fixed_replies"] = [{"keywords": "你好", "reply": "{name}你好呀", "enabled": True}]
        self.danmaku(app, "你好")
        await self.worker(app)
        item = await self.take(app)
        self.assertEqual(item["text"], "小明你好呀")
        app.brain.openai.ask.assert_not_called()
        self.assertIn("小明你好呀", player._chat_context())

    async def test_superchat_gets_game_context(self):
        app = make_app()
        player = attach_game(app)
        app.pipe.on_event({"type": "superchat", "uid": 123, "name": "小明", "text": "加油", "price": 30})
        await self.worker(app)
        self.assertEqual((await self.take(app))["type"], "superchat")
        self.assertIn("当前直播游戏上下文", app.brain.openai.ask.call_args.args[2])
        self.assertIn("加油", player._chat_context())

    def test_old_chat_and_state_are_not_presented_as_current(self):
        app = make_app()
        player = attach_game(app)
        player._last_state = game_state()
        player._state_time = time.monotonic() - 31
        self.assertNotIn("12/60", player.commentary_context())
        for n in range(10):
            player.remember_interaction({"name": "小明", "text": str(n)}, "收到")
        self.assertEqual(len(player.live_chat), 6)
        for item in player.live_chat:
            item["at"] -= 121
        self.assertEqual(player._chat_context(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
