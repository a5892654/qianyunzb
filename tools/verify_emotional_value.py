"""情绪接话、降级、播放状态与跨观众隔离；不连接直播、模型或声卡。"""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain import Brain
from config import DEFAULTS, merge
from conversation import Conversation
from dtgame.commentary import GameCommentary
from dtgame.player import Player
from dtgame.stardew import StardewPlayer
from emotional_value import EMOTIONAL_VALUE_PROMPT, viewer_cue
from main import App, build_web
from tools.verify_proactive_interaction import make_app, settle, wait_until, InteractionTestCase


def message(text, uid=123):
    return {"type": "danmaku", "uid": uid, "name": "小明", "text": text}


class CueTests(unittest.TestCase):
    def test_self_disclosures_requests_and_good_news_have_different_directions(self):
        cases = {
            "今天我有点难过": "comfort", "我今天被裁员了": "comfort", "我没考上": "comfort",
            "我没有拿到offer": "comfort", "抱抱我": "comfort", "求安慰": "comfort",
            "我好累": "rest", "今天好累": "rest", "刚下班": "rest",
            "今天是我生日": "birthday", "我今天生日": "birthday",
            "我面试通过了": "celebrate", "今天我拿到offer了": "celebrate", "夸夸我": "celebrate",
            "主播好可爱": "appreciation", "你的声音很好听": "appreciation",
            "我回来了": "welcome", "第一次来这里": "welcome",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(viewer_cue(text).kind, expected)

    def test_negations_other_people_and_game_terms_are_not_personal_distress(self):
        for text in ("我不难过了", "我没有那么累", "我没升职", "今天不是我生日",
                     "不要安慰我，讲这张牌就好", "你心情不好吗", "主播难过了吗",
                     "游戏角色累了", "为什么这个技能叫伤心箭", "你一点都不可爱"):
            with self.subTest(text=text):
                self.assertIsNone(viewer_cue(text))


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["ai"]["persona"] = "自定义：温和沉稳的电台主持人。"
        self.brain = Brain()

    async def test_both_backends_receive_current_feeling_without_extra_generation(self):
        for backend in ("openai", "qoder"):
            with self.subTest(backend=backend):
                self.cfg["ai"]["backend"] = backend
                model = AsyncMock(return_value="今天不好受吧，先在这儿缓一缓。")
                with patch.object(getattr(self.brain, backend), "ask", model):
                    text, source, _ = await self.brain.reply(
                        self.cfg, self.cfg["events"]["danmaku"], message("今天我有点难过"))
                model.assert_awaited_once()
                self.assertEqual(source, "AI")
                self.assertIn("缓一缓", text)
                _, system, prompt = model.await_args.args
                self.assertIn(self.cfg["ai"]["persona"], system)
                self.assertIn(EMOTIONAL_VALUE_PROMPT, system)
                self.assertIn("先温和接住感受", prompt)
                self.assertIn("今天我有点难过", prompt)

    async def test_failure_and_empty_text_give_relevant_varied_replies_and_valid_emotions(self):
        self.cfg["ai"]["backend"] = "openai"
        self.cfg["live2d"].update(enabled=True, emotions=[{"name": "温柔"}, {"name": "开心"}])
        for outcome in (RuntimeError("离线"), ""):
            with self.subTest(outcome=outcome):
                self.brain.openai.ask = AsyncMock()
                self.brain.openai.ask.side_effect = outcome if isinstance(outcome, Exception) else None
                self.brain.openai.ask.return_value = ""
                lines = []
                for _ in range(3):
                    text, _, emotion = await self.brain.reply(
                        self.cfg, self.cfg["events"]["danmaku"], message("我心情不好"))
                    self.assertEqual(emotion, "温柔")
                    self.assertNotIn("说：", text)
                    self.assertNotIn("我心情不好", text)
                    self.assertNotIn("关注", text)
                    lines.append(text)
                self.assertEqual(len(set(lines)), 3)
                self.assertEqual(self.brain.openai.ask.await_count, 3)
        self.brain.openai.ask = AsyncMock(return_value="")
        text, _, emotion = await self.brain.reply(
            self.cfg, self.cfg["events"]["danmaku"], message("今天是我生日"))
        self.assertIn("生日快乐", text)
        self.assertEqual(emotion, "开心")

    async def test_explicit_model_expression_custom_templates_and_disable_are_respected(self):
        self.cfg["ai"]["backend"] = "openai"
        self.cfg["live2d"]["enabled"] = True
        self.brain.openai.ask = AsyncMock(return_value="【感谢】这句夸奖我收下了。")
        _, _, emotion = await self.brain.reply(self.cfg, self.cfg["events"]["danmaku"], message("主播好可爱"))
        self.assertEqual(emotion, "感谢")
        self.brain.openai.ask.reset_mock()
        settings = {**self.cfg["events"]["danmaku"], "mode": "template", "templates": "自定义回应{name}"}
        self.assertEqual((await self.brain.reply(self.cfg, settings, message("我好累")))[0], "自定义回应小明")
        self.brain.openai.ask.assert_not_awaited()
        self.cfg["interaction"]["emotional_value"] = False
        self.brain.openai.ask.return_value = ""
        settings = {**settings, "mode": "ai"}
        text, _, _ = await self.brain.reply(self.cfg, settings, message("我好累"))
        self.assertEqual(text, "自定义回应小明")
        _, system, prompt = self.brain.openai.ask.await_args.args
        self.assertNotIn(EMOTIONAL_VALUE_PROMPT, system)
        self.assertNotIn("本条留言的接话方向", prompt)

    async def test_superchat_fallback_thanks_and_answers_the_feeling(self):
        self.cfg["ai"]["backend"] = "openai"
        self.brain.openai.ask = AsyncMock(side_effect=RuntimeError("离线"))
        ev = {**message("我好累"), "type": "superchat", "price": 30}
        text, _, _ = await self.brain.reply(self.cfg, self.cfg["events"]["superchat"], ev)
        self.assertIn("谢谢小明的留言", text)
        self.assertTrue(any(word in text for word in ("辛苦", "歇", "缓")))

    async def test_personal_reply_is_not_reused_in_other_viewers_variation_prompt(self):
        self.cfg["ai"]["backend"] = "openai"
        self.brain.openai.ask = AsyncMock(return_value="被裁员确实不好受，先缓一缓。")
        await self.brain.reply(self.cfg, self.cfg["events"]["danmaku"], message("我今天被裁员了"))
        self.brain.openai.ask.return_value = "这张牌可以先留着。"
        await self.brain.reply(self.cfg, self.cfg["events"]["danmaku"], message("这张牌怎么用", uid=456))
        self.assertNotIn("被裁员", self.brain.openai.ask.await_args.args[2])


class CareContextTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.chat = Conversation(clock=lambda: self.now[0])

    def test_personal_topic_stays_with_its_viewer_and_closes_without_repeating_details(self):
        ev = message("我今天被裁员了")
        self.chat.replied(ev, "今天不容易，先歇会儿。")
        self.assertEqual(self.chat.plan(), "close")
        self.assertIn("被裁员", self.chat.context(message("嗯，谢谢")))
        self.assertNotIn("被裁员", self.chat.context(message("你好", uid=456)))
        self.assertNotIn("被裁员", self.chat.prompt("close"))
        self.chat.replied(message("谢谢，先看会儿"), "想聊再聊。")
        self.assertTrue(self.chat.current()["private"])
        self.chat.replied(message("换个话题，这张牌怎么用"), "先看它的费用。")
        self.assertFalse(self.chat.current()["private"])
        self.assertNotIn("被裁员", self.chat.context(message("你好", uid=456)))
        self.now[0] += 301
        self.assertEqual(self.chat.context(ev), "")
        self.chat.reset()
        self.assertEqual(self.chat.care_until, 0)


class PipelineTests(InteractionTestCase):
    async def test_personal_feeling_does_not_become_game_commentary_material(self):
        app, _ = make_app()
        game = Mock(running=True)
        game.commentary_context.return_value = "正在打牌。"
        game.speech_state.return_value = {"mode": "battle", "label": "自然接话", "max_chars": 26}
        app.dt = game
        app.brain.openai.ask.return_value = "听见了，先缓一缓。"
        app.pipe.on_event(message("我今天被裁员了"))
        await self.workers(app, app.pipe._gen_worker)
        await asyncio.wait_for(app.pipe.synth_q.get(), 2)
        game.remember_interaction.assert_not_called()

    async def test_care_uses_actual_playback_and_proactive_chat_does_not_resurface_distress(self):
        app, now = make_app(follow=True)
        app.pipe.interaction.sync_room()
        now[0] += 181
        app.brain.openai.ask.return_value = "听起来今天不容易，先在这儿缓一缓。"
        app.pipe.on_event(message("我今天被裁员了"))
        gen_tasks = await self.workers(app, app.pipe._gen_worker)
        item = await asyncio.wait_for(app.pipe.synth_q.get(), 2)
        self.assertEqual(app.pipe.interaction.conversation.care_until, 0)
        await app.pipe.synth_q.put(item)
        audio_tasks = await self.workers(app, app.pipe._synth_worker, app.pipe._play_worker)
        await wait_until(lambda: any(kind == "speak_end" for kind, _ in app.pushes))
        await self.stop_tasks([*gen_tasks, *audio_tasks])
        interaction = app.pipe.interaction
        self.assertEqual(interaction.conversation.care_until, now[0] + 90)
        self.assertFalse(interaction.recent_speech)
        now[0] += 21
        app.brain.openai.ask.side_effect = RuntimeError("离线")
        interaction.tick()
        ev = app.pipe.jobs[0]["ev"]
        self.assertFalse(ev["follow_reminder"])
        self.assertEqual(ev["conversation_plan"], "close")
        text, _, _ = await interaction.reply(ev)
        self.assertFalse(Conversation.is_question(text))
        prompt = app.brain.openai.ask.await_args.args[2]
        self.assertNotIn("被裁员", prompt)
        self.assertNotIn("听起来今天不容易", prompt)
        interaction.spoken(text, ev)
        settle(app)
        now[0] += 70
        interaction.tick()
        self.assertTrue(app.pipe.jobs[0]["ev"]["follow_reminder"])

    async def test_room_switch_and_disabled_enhancement_do_not_keep_care_delay(self):
        app, now = make_app(follow=True)
        interaction = app.pipe.interaction
        interaction.sync_room()
        interaction.conversation.replied(message("我好累"), "先歇一会儿。")
        app.bili.room_id = 84
        interaction.sync_room()
        self.assertEqual(interaction.conversation.care_until, 0)
        self.assertIsNone(interaction.conversation.current())
        now[0] += 181
        interaction.conversation.replied(message("我好累"), "先歇一会儿。")
        app.cfg["interaction"]["emotional_value"] = False
        interaction.tick()
        self.assertTrue(app.pipe.jobs[0]["ev"]["follow_reminder"])

    async def test_toggle_updates_active_game_and_chat_without_resetting_game_decisions(self):
        app = object.__new__(App)
        app.cfg = copy.deepcopy(DEFAULTS)
        app.bili = Mock(client=None)
        app.game = Mock()
        app.brain = Brain()
        app.brain.openai.history.append({"role": "assistant", "content": "旧语气"})
        app.dt = Player(app._dt_ai(), Mock())
        decision = {"role": "assistant", "content": '{"action":"end"}'}
        app.dt.history.append(decision)
        client = TestClient(TestServer(build_web(app)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        with patch("config.save"):
            response = await client.post("/api/config", json={"interaction": {"emotional_value": False}},
                                         headers={"X-BVB": "1"})
        self.assertEqual(response.status, 200)
        self.assertNotIn(EMOTIONAL_VALUE_PROMPT, app.brain.system_prompt(app.cfg))
        self.assertNotIn(EMOTIONAL_VALUE_PROMPT, app.dt._system_prompt())
        self.assertEqual(app.dt.history, [decision])
        self.assertFalse(app.brain.openai.history)
        self.assertTrue(merge(DEFAULTS, {"interaction": {"enabled": False}})["interaction"]["emotional_value"])


class GameFeelingTests(unittest.TestCase):
    def test_both_game_players_share_the_toggle_and_keep_their_game_rules(self):
        app = object.__new__(App)
        app.cfg = copy.deepcopy(DEFAULTS)
        for on in (True, False):
            app.cfg["interaction"]["emotional_value"] = on
            for game in (Player(app._dt_ai(), Mock()), StardewPlayer(app._stardew_ai(), Mock())):
                with self.subTest(on=on, game=type(game).__name__):
                    prompt = game._system_prompt()
                    self.assertEqual(EMOTIONAL_VALUE_PROMPT in prompt, on)
                    self.assertIn("JSON", prompt)

    def test_emotional_reaction_is_attached_to_confirmed_moments_and_can_be_disabled(self):
        commentary = GameCommentary()
        state = {"screen": "battle", "run": {"run_id": "1", "hp": 10, "max_hp": 60},
                 "battle": {"battle_id": "1", "hp": 10, "max_hp": 60, "enemies": [{"hp": 10}]}}
        commentary.observe(state)
        self.assertNotIn("胜利与大家分享", commentary.prompt(state))
        commentary.spoken(state, "先稳住。")
        won = copy.deepcopy(state)
        won["battle"].update(victory=True, battle_over=True, enemies=[{"hp": 0}])
        commentary.observe(won)
        self.assertIn("先松一口气", commentary.prompt(won))
        self.assertNotIn("这一刻的情绪", commentary.prompt(won, emotional_value=False))
        self.assertIn("已经确认战斗获胜", commentary.prompt(won, emotional_value=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
