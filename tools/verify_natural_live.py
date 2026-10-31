"""连续接话、真实局势反应与忙闲节奏；只用模拟模型、游戏桥和声卡。"""
import asyncio
import copy
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conversation import Conversation
from dtgame.commentary import GameCommentary, scene
from dtgame.player import Player
from tools.verify_proactive_interaction import make_app, enter, settle, wait_until, InteractionTestCase


def state(hp=50, *, screen="battle", turn=1, **battle):
    return {"screen": screen, "run": {"run_id": "run1", "character": "奈尔", "act": 1,
                                       "floor": 2, "hp": hp, "max_hp": 60, "relics": []},
            "battle": {"battle_id": "fight1", "hp": hp, "max_hp": 60, "turn": turn,
                       "enemies": [{"id": "slime", "hp": 20}], **battle}}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.chat = Conversation(clock=lambda: self.now[0])
        self.ev = {"type": "danmaku", "uid": 123, "name": "小明", "text": "我喜欢防御流"}

    def test_question_is_remembered_and_unanswered_question_closes_once(self):
        self.chat.spoken("你们喜欢防御流还是输出流？", "new")
        self.assertIn("你们喜欢防御流还是输出流", self.chat.context(self.ev))
        self.assertEqual(self.chat.plan(), "close")
        self.chat.spoken("咱们慢慢玩，想到什么再聊。", "close")
        self.assertEqual(self.chat.plan(), "new")

    def test_viewer_answer_continues_then_followup_does_not_keep_asking(self):
        self.chat.spoken("你们喜欢什么打法？", "new")
        self.chat.replied(self.ev, "原来你喜欢防御流，这把可以留意格挡牌。")
        self.assertEqual(self.chat.plan(), "continue")
        self.chat.spoken("刚才说到防御流，慢慢成型也挺有意思。", "continue")
        self.assertEqual(self.chat.plan(), "new")

    def test_turn_limit_new_subject_and_expiry(self):
        for _ in range(4):
            self.chat.replied(self.ev, "这个想法可以试试。")
        self.assertEqual(self.chat.plan(), "close")
        self.chat.replied({**self.ev, "text": "换个话题，今天吃了什么"}, "聊聊吃的也好。")
        self.assertEqual(self.chat.current()["turns"], 1)
        self.assertIn("吃了什么", self.chat.current()["opening"])
        self.now[0] += 301
        self.assertEqual(self.chat.context(self.ev), "")

    def test_same_display_name_does_not_share_personal_history(self):
        self.chat.replied(self.ev, "你喜欢防御流，我记下这段话。")
        self.chat.topic = None
        self.assertEqual(self.chat.context({**self.ev, "uid": 456}), "")
        self.assertIn("我喜欢防御流", self.chat.context(self.ev))


class GameMomentTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.chat = GameCommentary(clock=lambda: self.now[0])

    def test_damage_low_health_and_healing_use_actual_delta_and_do_not_repeat(self):
        self.chat.observe(state())
        self.assertIsNone(self.chat.moment())
        self.chat.observe(state(10, turn=2))
        self.assertEqual(self.chat.moment()["kind"], "low_health")
        self.assertIn("当前生命", self.chat.prompt(state(10)))
        self.chat.spoken(state(10), "先稳住！")
        self.chat.observe(state(10, turn=2))
        self.assertIsNone(self.chat.moment())
        self.chat.observe(state(30, turn=3))
        self.assertEqual(self.chat.moment()["kind"], "heal")
        self.assertEqual(self.chat.moment()["facts"]["生命变化"], 20)

    def test_only_confirmed_victory_can_celebrate_and_low_health_win_is_distinct(self):
        self.chat.observe(state(10))
        self.chat.spoken(state(10), "有点悬！")
        self.chat.observe(state(10, awaiting_confirm=True))
        self.assertIsNone(self.chat.moment())
        won = state(10, battle_over=True, enemies=[{"id": "slime", "hp": 0}])
        self.chat.observe(won)
        self.assertEqual(self.chat.moment()["kind"], "clutch_win")
        self.chat.observe(won)
        self.assertEqual(self.chat.moment()["kind"], "clutch_win")
        self.chat.spoken(won, "终于挺过来了！")
        self.chat.observe(won)
        self.assertIsNone(self.chat.moment())

    def test_boss_death_and_reward_are_grounded(self):
        boss = state(type="boss")
        self.chat.observe(boss)
        self.assertEqual(self.chat.moment()["kind"], "boss")
        self.chat.spoken(boss, "先认真看一下。")
        self.chat.observe(state(0))
        self.assertIsNone(self.chat.moment())  # 零血可能免死，不提前宣称阵亡。
        self.chat.observe(state(0, screen="death"))
        self.assertEqual(self.chat.moment()["kind"], "death")
        self.chat.spoken(state(0, screen="death"), "这波没撑住。")
        self.chat.observe(state(0, screen="death_analysis"))
        self.assertIsNone(self.chat.moment())

    def test_offered_reward_is_not_received_reward_but_actual_relic_gain_is(self):
        reward = state(screen="reward")
        reward.pop("battle")
        reward["reward"] = {"offers": [{"name": "传说奖励", "rarity": "legendary"}]}
        self.chat.observe(reward)
        self.assertIsNone(self.chat.moment())
        gained = copy.deepcopy(reward)
        gained["run"]["relics"] = ["护符"]
        self.chat.observe(gained)
        self.assertEqual(self.chat.moment()["kind"], "relic")
        self.assertEqual(self.chat.moment()["facts"], {"新获得遗物": ["护符"]})

    def test_stale_moment_and_new_run_do_not_leak(self):
        self.chat.observe(state(10))
        self.now[0] += 21
        self.assertIsNone(self.chat.moment())
        fresh = state()
        fresh["run"]["run_id"] = "run2"
        self.chat.observe(fresh)
        self.assertIsNone(self.chat.moment())

    def test_normal_cards_have_no_artificial_silence_and_real_moment_can_react(self):
        calm = state()
        self.assertTrue(self.chat.can_speak(calm, "这张可以接上。"))
        self.chat.spoken(calm, "这张可以接上。")
        self.assertTrue(self.chat.can_speak(calm, "下一张也接上。"))
        self.assertTrue(self.chat.can_speak(state(10), "哎哟！", important=True))
        self.now[0] += 10
        self.assertTrue(self.chat.can_speak(calm, "下一张也接上。"))

    def test_scene_distinguishes_battle_danger_boss_and_rest(self):
        self.assertEqual(scene(state())["mode"], "battle")
        self.assertEqual(scene(state(10))["max_chars"], 18)
        self.assertEqual(scene(state(type="boss"))["mode"], "tense")
        self.assertEqual(scene(state(enemies=[{"intent": {"type": "attack", "value": 60}}]))["mode"], "tense")
        self.assertEqual(scene({"screen": "reward"})["mode"], "rest")


class NaturalPipelineTests(InteractionTestCase):
    async def test_spoken_question_and_same_viewer_answers_flow_through_real_pipeline(self):
        app, now = make_app()
        app.cfg["events"]["danmaku"]["cooldown_user"] = 0
        app.brain.openai.ask.side_effect = ["你们喜欢什么打法？", "看来你喜欢防御流，慢慢叠甲也挺舒服。", "那我们就留意格挡牌，找找成型的机会。"]
        enter(app)
        settle(app)
        now[0] += 21
        app.pipe.interaction.tick()
        await self.workers(app, app.pipe._gen_worker, app.pipe._synth_worker, app.pipe._play_worker)
        await wait_until(lambda: sum(k == "speak_end" for k, _ in app.pushes) == 1)
        for index, text in enumerate(("我喜欢防御流", "对啊，慢慢叠"), 2):
            app.pipe.on_event({"type": "danmaku", "uid": 123, "name": "小明", "text": text})
            await wait_until(lambda: sum(k == "speak_end" for k, _ in app.pushes) == index)
        prompt = app.brain.openai.ask.await_args.args[2]
        for expected in ("你们喜欢什么打法", "我喜欢防御流", "看来你喜欢防御流", "对啊，慢慢叠", "连续对话"):
            self.assertIn(expected, prompt)
        self.assertEqual(app.brain.openai.ask.await_count, 3)
        self.assertEqual(app.pipe.interaction.conversation.current()["turns"], 3)

    async def test_generated_but_unplayed_reply_is_not_added_to_conversation(self):
        app, _ = make_app()
        app.pipe.on_event({"type": "danmaku", "uid": 123, "name": "小明", "text": "喜欢防御流"})
        await self.workers(app, app.pipe._gen_worker)
        item = await asyncio.wait_for(app.pipe.synth_q.get(), 1)
        self.assertFalse(app.pipe.interaction.conversation.turns)
        self.assertIn("conversation_event", item)

    async def test_no_answer_closes_without_another_question(self):
        app, now = make_app()
        enter(app)
        settle(app)
        interaction = app.pipe.interaction
        interaction.spoken("你们喜欢什么游戏？", {"conversation_plan": "new"})
        now[0] += 61
        interaction.tick()
        ev = app.pipe.jobs[0]["ev"]
        self.assertEqual(ev["conversation_plan"], "close")
        text, _, _ = await interaction.reply(ev)
        self.assertFalse(Conversation.is_question(text))
        interaction.spoken(text, ev)
        self.assertEqual(interaction.conversation.plan(), "new")

    async def test_tense_scene_keeps_interaction_and_viewer_reply_is_short_and_immediate(self):
        app, now = make_app(follow=True)
        enter(app)
        settle(app)
        app.dt = SimpleNamespace(running=True, speech_state=lambda: scene(state(10)),
                                 commentary_context=lambda: "实际生命10/60", remember_interaction=Mock())
        now[0] += 181
        app.pipe.interaction.tick()
        self.assertTrue(app.pipe.jobs[0]["ev"]["follow_reminder"])
        settle(app)
        app.pipe.on_event({"type": "danmaku", "uid": 123, "name": "小明", "text": "先回血"})
        await self.workers(app, app.pipe._gen_worker)
        item = await asyncio.wait_for(app.pipe.synth_q.get(), 1)
        self.assertEqual(item["type"], "danmaku")
        cfg, system, prompt = app.brain.openai.ask.await_args.args
        self.assertEqual(cfg["reply_max_chars"], 18)
        self.assertEqual(cfg["max_chars"], app.cfg["ai"]["max_chars"])
        self.assertIn("本句约18字", prompt)
        self.assertEqual(system, app.brain.system_prompt(app.cfg))

    async def test_configured_interval_has_no_extra_random_wait(self):
        app, now = make_app()
        interaction = app.pipe.interaction
        enter(app)
        settle(app)
        now[0] += 21
        interaction.tick()
        ev = app.pipe.jobs[0]["ev"]
        interaction.spoken("一起慢慢玩。", ev)
        settle(app)
        now[0] += 59
        for _ in range(8):
            interaction.tick()
        self.assertFalse(app.pipe.jobs)
        now[0] += 1
        interaction.tick()
        self.assertEqual(len(app.pipe.jobs), 1)

    async def test_battle_does_not_indefinitely_block_follow_reminder(self):
        app, now = make_app(follow=True)
        enter(app)
        settle(app)
        current = [state()]
        app.dt = SimpleNamespace(running=True, speech_state=lambda: scene(current[0]))
        now[0] += 181
        app.pipe.interaction.tick()
        self.assertTrue(app.pipe.jobs[0]["ev"]["follow_reminder"])
        settle(app)
        now[0] += 61
        current[0] = {"screen": "reward"}
        app.pipe.interaction.tick()
        self.assertTrue(app.pipe.jobs[0]["ev"]["follow_reminder"])

    async def test_follow_reminder_respects_interval_without_additional_delay(self):
        app, now = make_app(follow=True)
        interaction = app.pipe.interaction
        enter(app)
        settle(app)
        now[0] += 179
        # 禁用普通找话题，单独验证提醒期限不会提前。
        app.cfg["interaction"]["enabled"] = False
        interaction.tick()
        self.assertFalse(app.pipe.jobs)
        now[0] += 2
        interaction.tick()
        self.assertTrue(app.pipe.jobs[0]["ev"]["follow_reminder"])
        settle(app)
        app.cfg["interaction"]["enabled"] = True
        ev = {"type": "danmaku", "uid": 123, "name": "小明", "text": "喜欢叠甲"}
        interaction.conversation.replied(ev, "叠起来很有安全感。")
        now[0] += 61
        interaction.tick()
        self.assertTrue(app.pipe.jobs[0]["ev"]["follow_reminder"])

    async def test_expired_reaction_after_synthesis_is_dropped_before_audio(self):
        app, _ = make_app()
        job = app.pipe.react("哎哟，这下掉血有点疼。")
        await self.workers(app, app.pipe._gen_worker)
        item = await asyncio.wait_for(app.pipe.synth_q.get(), 1)
        item["expires_at"] = time.monotonic() - 1
        await app.pipe.play_q.put(item)
        await self.workers(app, app.pipe._play_worker)
        await wait_until(lambda: app.pipe.spoken_done(job))
        app.player.play_blocking.assert_not_called()

    async def test_game_reaction_uses_real_changes_and_normal_cards_keep_talking(self):
        app, now = make_app()
        bridge = Mock()
        bridge.screen.return_value = {"screen": "battle"}
        bridge.end_turn.return_value = {"ok": True}
        player = Player({}, bridge, pipe=app.pipe, speak=app.pipe.narrate)
        player.commentary.clock = lambda: now[0]
        player._ask = AsyncMock(side_effect=[{"action": "end", "say": "先稳稳接上。"},
                                            {"action": "end", "say": "这一张继续。"},
                                            {"action": "end", "say": "哎哟，这下有点疼。"}])
        await player.step(state())
        second = await player.step(state(turn=2))
        self.assertEqual(second["say"], "这一张继续。")
        third = await player.step(state(10, turn=3))
        self.assertIn("疼", third["say"])
        self.assertEqual([j["type"] for j in app.pipe.jobs], ["narration", "narration", "reaction"])
        self.assertIn("当前生命", player._ask.await_args.args[0])
        bridge.state.assert_not_called()
        self.assertEqual(bridge.end_turn.call_count, 3)

    async def test_confirmed_win_reacts_once_without_extra_model_call(self):
        app, _ = make_app()
        bridge = Mock()
        bridge.call.return_value = {"ok": True}
        player = Player({}, bridge, pipe=app.pipe, speak=app.pipe.narrate)
        player._ask = AsyncMock()
        won = state(8, victory=True, battle_over=True, awaiting_confirm=True)
        await player.step(won)
        await player.step(won)
        player._ask.assert_not_awaited()
        self.assertEqual(len(app.pipe.jobs), 1)
        self.assertEqual(app.pipe.jobs[0]["type"], "reaction")
        self.assertIn("悬", app.pipe.jobs[0]["text"])

    async def test_stale_decision_and_expired_reaction_do_not_play(self):
        app, _ = make_app()
        bridge = Mock()
        bridge.screen.return_value = {"screen": "reward"}
        player = Player({}, bridge, pipe=app.pipe, speak=app.pipe.narrate)
        player._ask = AsyncMock(return_value={"action": "end", "say": "哎哟！"})
        self.assertTrue((await player.step(state(8)))["stale"])
        self.assertFalse(app.pipe.jobs)
        job = app.pipe.react("刚才有点悬。")
        app.pipe.jobs[0]["ev"]["expires_at"] = time.monotonic() - 1
        await self.workers(app, app.pipe._gen_worker, app.pipe._synth_worker, app.pipe._play_worker)
        await wait_until(lambda: app.pipe.spoken_done(job))
        app.player.play_blocking.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
