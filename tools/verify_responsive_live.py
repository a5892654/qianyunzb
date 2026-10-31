"""延迟与跑题回归：连续解说、新弹幕抢占后台闲聊、白饭仅按需读取。"""
import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain import Brain
from config import DEFAULTS
from dtgame.player import Player
from live_context import mentions_rice, rice_question
from tools.verify_natural_live import state
from tools.verify_proactive_interaction import InteractionTestCase, enter, make_app, settle, wait_until


class ResponsiveLiveTests(InteractionTestCase):
    async def test_new_viewer_message_interrupts_slow_background_generation(self):
        app, now = make_app()
        enter(app)
        settle(app)
        now[0] += 21
        app.pipe.interaction.tick()
        background = app.pipe.jobs[0]["id"]
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def model(*args):
            if not started.is_set():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return "马上看这条弹幕，先把血稳住。"

        app.brain.openai.ask.side_effect = model
        await self.workers(app, app.pipe._gen_worker)
        await asyncio.wait_for(started.wait(), 1)
        app.pipe.on_event({"type": "danmaku", "uid": 456, "name": "小红", "text": "记得回血"})
        item = await asyncio.wait_for(app.pipe.synth_q.get(), 1)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(app.pipe.spoken_done(background))
        self.assertEqual(item["type"], "danmaku")
        self.assertEqual(app.brain.openai.ask.await_count, 2)

    async def test_empty_or_off_topic_game_lines_do_not_create_silent_actions(self):
        app, now = make_app()
        bridge = Mock()
        bridge.screen.return_value = {"screen": "battle"}
        bridge.end_turn.return_value = {"ok": True}
        player = Player({}, bridge, pipe=app.pipe, speak=app.pipe.narrate)
        player.commentary.clock = lambda: now[0]
        answers = iter(("", "左下角白饭有三碗。", "", ""))
        player._ask = AsyncMock(side_effect=lambda text: {"action": "end", "say": next(answers)})
        spoken = []
        for turn in range(1, 5):
            result = await player.step(state(turn=turn))
            spoken.append(result["say"])
        self.assertEqual(len(app.pipe.jobs), 4)
        self.assertTrue(all(spoken))
        self.assertTrue(all(a != b for a, b in zip(spoken, spoken[1:])))
        self.assertFalse(any(mentions_rice(line) for line in spoken))
        self.assertEqual(player._ask.await_count, 4)

    async def test_rice_history_does_not_seed_proactive_topics(self):
        app, now = make_app()
        enter(app)
        settle(app)
        interaction = app.pipe.interaction
        ev = {"type": "danmaku", "name": "小明", "text": "左下角是什么？", "_interaction_epoch": interaction.epoch}
        interaction.remember_reply(ev, "这是大碗白饭，现在三碗。")
        interaction.remember_spoken_reply(ev, "这是大碗白饭，现在三碗。")
        interaction.remember_spoken("这是大碗白饭，现在三碗。")
        self.assertFalse(interaction.dialogue)
        self.assertFalse(interaction.conversation.turns)
        self.assertFalse(interaction.recent_speech)
        app.brain.live_context = Mock(return_value="不应读取的白饭素材信息")
        app.brain.openai.ask.return_value = "左下角的白饭又有变化了！"
        now[0] += 21
        interaction.tick()
        text, _, _ = await interaction.reply(app.pipe.jobs[0]["ev"])
        app.brain.live_context.assert_not_called()
        self.assertFalse(mentions_rice(text))
        self.assertNotIn("白饭", app.brain.openai.ask.await_args.args[2])
        self.assertEqual(app.brain.openai.ask.await_count, 1)


class RiceIsolationTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["ai"]["backend"] = "openai"
        return cfg

    def test_query_routing_uses_current_viewer_text(self):
        for text in ("左下角是什么？", "饭碗现在几碗", "怎么加碗", "那个白饭还在吗"):
            self.assertTrue(rice_question(text), text)
        for text in ("主播你好", "今天吃什么", "左下角的技能是什么", "先把血回上来"):
            self.assertFalse(rice_question(text), text)

    async def test_qoder_rice_answers_use_a_separate_session(self):
        cfg = self.config()
        cfg["ai"]["backend"] = "qoder"
        provider = lambda question: "大碗白饭，当前三碗" if rice_question(question) else ""
        brain = Brain(live_context=provider)
        brain.qoder.ask = AsyncMock(return_value="咱们接着打这手。")
        brain.rice_qoder.ask = AsyncMock(return_value="左下角是白饭计数，现在三碗。")
        await brain.ai(cfg, "回复观众你好", question="你好")
        await brain.ai(cfg, "回复观众提问", question="左下角那个是什么？")
        await brain.ai(cfg, "继续解说")
        self.assertEqual(brain.qoder.ask.await_count, 2)
        self.assertEqual(brain.rice_qoder.ask.await_count, 1)
        self.assertNotIn("白饭", brain.qoder.ask.await_args.args[2])

    async def test_rice_request_and_answer_never_enter_regular_api_history(self):
        cfg = self.config()
        brain = Brain(live_context=lambda question: "当前白饭三碗" if rice_question(question) else "")
        payloads = []

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self, **kwargs):
                return {"choices": [{"message": {"content": "自然聊一句。"}}]}

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def post(self, url, **kwargs):
                payloads.append(kwargs["json"])
                return Response()

        with patch("brain.aiohttp.ClientSession", return_value=Session()):
            await brain.ai(cfg, "你好", question="你好")
            await brain.ai(cfg, "饭碗是什么", question="饭碗是什么")
            await brain.ai(cfg, "接着聊游戏")
        self.assertIn("白饭", json.dumps(payloads[1], ensure_ascii=False))
        self.assertNotIn("饭碗", json.dumps(payloads[2], ensure_ascii=False))
        self.assertNotIn("白饭", json.dumps(payloads[2], ensure_ascii=False))
        self.assertEqual(len(brain.openai.history), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
