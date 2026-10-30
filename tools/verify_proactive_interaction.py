"""主动互动离线回归：可控时钟、真实事件队列、模拟模型与音频设备。"""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bili import _Handler
from blivedm.models.web import InteractWordV2Message
from brain import Brain
from config import DEFAULTS, FOLLOW_REMINDER_TEMPLATES, merge
from pipeline import Pipeline


def make_app(*, follow=False):
    now = [100.0]
    cfg = copy.deepcopy(DEFAULTS)
    cfg["ai"]["backend"] = "openai"
    cfg["interaction"]["follow_enabled"] = follow
    cfg["events"]["enter"].update(merge_window=0, templates="欢迎{name}")
    cfg["tts"].update(stream_enabled=False)
    app = SimpleNamespace(cfg=cfg, pushes=[], dt=None, brain=Brain(), songs=Mock(),
                          bili=SimpleNamespace(connected=True, room_id=42, client=object(), owner_uid=999),
                          schedule=SimpleNamespace(active_action=""), broadcast=SimpleNamespace(phase="live"))
    app.songs.on_danmaku.return_value = False
    app.push = lambda kind, data: app.pushes.append((kind, dict(data)))
    app.brain.openai.ask = AsyncMock(return_value="你们觉得这局应该稳一点，还是试试更大胆的打法？")
    app.engine = SimpleNamespace(synth=AsyncMock(return_value=(np.zeros((240, 1), dtype=np.int16), 24000)))
    app.player = Mock()
    app.player.voice_clock.return_value = 0
    app.pipe = Pipeline(app, supporters=Mock(contains=Mock(return_value=False)))
    app.pipe.interaction.clock = lambda: now[0]
    app.pipe.interaction.jitter = lambda low, high: low  # 固定时钟用最短合法间隔；随机节奏另行验证。
    return app, now


def enter(app, **overrides):
    app.pipe.on_event({"type": "enter", "uid": 123, "name": "小明", **overrides})


def settle(app):
    app.pipe.clear()


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


class InteractionTestCase(unittest.IsolatedAsyncioTestCase):
    async def workers(self, app, *workers):
        tasks = [asyncio.create_task(worker()) for worker in workers]
        self.addAsyncCleanup(self.stop_tasks, tasks)
        return tasks

    @staticmethod
    async def stop_tasks(tasks):
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class ProactiveInteractionTests(InteractionTestCase):
    def active(self):
        app, now = make_app()
        enter(app)
        settle(app)
        now[0] += 21
        return app, now

    async def test_no_activity_and_simulated_filtered_events_do_not_trigger(self):
        app, now = make_app()
        for event in ({"test": True}, {"uid": 999}, {"name": "屏蔽用户"}):
            app.cfg["filter"]["ignore_users"] = "屏蔽用户"
            enter(app, **event)
        settle(app)
        now[0] += 100
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        self.assertFalse(app.pipe.interaction.viewers)

    async def test_waits_for_cold_room_and_prevents_duplicate_queueing(self):
        app, now = make_app()
        enter(app)
        settle(app)
        now[0] += 19
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        now[0] += 2
        for _ in range(10):
            app.pipe.interaction.tick()
        self.assertEqual([j["type"] for j in app.pipe.jobs], ["interaction"])
        settle(app)
        now[0] += 59
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        now[0] += 2
        app.pipe.interaction.tick()
        self.assertEqual(len(app.pipe.jobs), 1)

    async def test_background_ticker_automatically_queues_interaction(self):
        app, _ = self.active()
        await self.workers(app, app.pipe._ticker)
        await wait_until(lambda: bool(app.pipe.jobs))
        self.assertEqual(app.pipe.jobs[0]["type"], "interaction")

    async def test_presence_expires_and_disconnect_or_room_switch_clears_context(self):
        app, now = self.active()
        now[0] += 300
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        for change in (lambda a: setattr(a.bili, "connected", False),
                       lambda a: setattr(a.bili, "room_id", 84),
                       lambda a: setattr(a.bili, "client", object())):
            app, now = self.active()
            app.pipe.interaction.dialogue.append({"at": now[0], "text": "旧房间"})
            change(app)
            app.pipe.interaction.tick()
            self.assertFalse(app.pipe.jobs)
            self.assertFalse(app.pipe.interaction.dialogue)

    async def test_pausing_disabling_and_farewell_suppress_interaction(self):
        for change in (lambda a: setattr(a.pipe, "paused", True),
                       lambda a: a.cfg["interaction"].update(enabled=False),
                       lambda a: setattr(a.schedule, "active_action", "farewell"),
                       lambda a: setattr(a.broadcast, "phase", "stopping")):
            app, _ = self.active()
            change(app)
            app.pipe.interaction.tick()
            self.assertFalse(app.pipe.jobs)

    async def test_waits_for_real_reply_to_finish_even_after_leaving_job_queue(self):
        app, now = make_app()
        enter(app)
        job = app.pipe.jobs.pop()
        now[0] += 60
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        app.pipe._mark_done(job["id"])
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        now[0] += 21
        app.pipe.interaction.tick()
        self.assertEqual(app.pipe.jobs[0]["type"], "interaction")

    async def test_real_entry_and_voice_pipeline_interact_before_game_narration(self):
        app, now = make_app()
        _Handler(app.pipe.on_event)._on_interact_word_v2(None,
            InteractWordV2Message(msg_type=1, uid=123, username="小明"))
        welcome = app.pipe.jobs[0]["id"]
        await self.workers(app, app.pipe._gen_worker, app.pipe._synth_worker, app.pipe._play_worker)
        await wait_until(lambda: app.pipe.spoken_done(welcome))
        now[0] += 21
        app.pipe.narrate("先打这张牌")
        app.pipe.interaction.tick()
        await wait_until(lambda: sum(k == "speak_end" for k, _ in app.pushes) == 3)
        spoken = [data["text"] for kind, data in app.pushes if kind == "speak"]
        self.assertEqual(spoken, ["欢迎小明", app.brain.openai.ask.return_value, "先打这张牌"])
        self.assertEqual(app.player.play_blocking.call_count, 3)

    async def test_proactive_prompt_uses_recent_dialogue_and_current_game(self):
        app, now = self.active()
        ev = {"type": "danmaku", "name": "小明", "text": "喜欢防御流", "_interaction_epoch": app.pipe.interaction.epoch}
        app.pipe.interaction.remember_reply(ev, "这局试试叠甲")
        app.dt = SimpleNamespace(running=True, commentary_context=lambda: "生命12/60，目前手里有防御牌")
        app.pipe.interaction.tick()
        text, _, _ = await app.pipe.interaction.reply(app.pipe.jobs[0]["ev"])
        prompt = app.brain.openai.ask.await_args.args[2]
        for expected in ("喜欢防御流", "叠甲", "12/60", "不要声称知道实时在线人数"):
            self.assertIn(expected, prompt)
        self.assertEqual(text, app.brain.openai.ask.return_value)

    async def test_new_message_during_generation_suppresses_proactive_result(self):
        app, now = self.active()
        app.pipe.interaction.tick()
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args):
            started.set()
            await release.wait()
            return "大家喜欢什么游戏？"

        app.brain.openai.ask.side_effect = delayed
        request = asyncio.create_task(app.pipe.interaction.reply(app.pipe.jobs[0]["ev"]))
        await started.wait()
        app.pipe.on_event({"type": "danmaku", "uid": 456, "name": "小红", "text": "主播你好"})
        release.set()
        text, _, _ = await request
        self.assertEqual(text, "")

    async def test_already_generated_interaction_is_dropped_before_playback_on_farewell(self):
        app, _ = self.active()
        app.pipe.interaction.tick()
        await self.workers(app, app.pipe._gen_worker)
        item = await asyncio.wait_for(app.pipe.synth_q.get(), 1)
        app.schedule.active_action = "farewell"
        await app.pipe.play_q.put(item)
        await self.workers(app, app.pipe._play_worker)
        await wait_until(lambda: app.pipe.spoken_done(item["id"]))
        app.player.play_blocking.assert_not_called()

    async def test_model_failure_or_repetition_rotates_fallback_topics(self):
        app, now = self.active()
        app.brain.openai.ask.side_effect = RuntimeError("模型离线")
        lines = []
        for _ in range(3):
            app.pipe.interaction.tick()
            ev = app.pipe.jobs[0]["ev"]
            text, _, _ = await app.pipe.interaction.reply(ev)
            lines.append(text)
            app.pipe.interaction.spoken(text)
            settle(app)
            now[0] += 61
        self.assertEqual(len(set(lines)), 3)
        app.brain.openai.ask.side_effect = None
        app.brain.openai.ask.return_value = lines[-1]
        app.pipe.interaction.tick()
        text, _, _ = await app.pipe.interaction.reply(app.pipe.jobs[0]["ev"])
        self.assertNotEqual(text, lines[-1])

    async def test_zero_intervals_are_clamped_to_prevent_spam(self):
        app, now = make_app()
        app.cfg["interaction"].update(idle_seconds=0, interval_seconds=0)
        enter(app)
        settle(app)
        now[0] += 9
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        now[0] += 2
        app.pipe.interaction.tick()
        self.assertEqual(len(app.pipe.jobs), 1)
        settle(app)
        now[0] += 29
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)

    async def test_near_duplicate_is_rewritten_and_recent_spoken_chat_is_in_prompt(self):
        app, _ = self.active()
        previous = "大家玩游戏时更喜欢稳一点，还是大胆试试新打法？"
        revised = "小时候玩过的游戏里，哪一段背景音乐最难忘？"
        app.pipe.interaction.remember_spoken(previous)
        app.brain.openai.ask.side_effect = [
            "大家玩游戏时更喜欢稳一点，还是大胆试试新打法呀？", revised]
        app.pipe.interaction.tick()
        text, _, _ = await app.pipe.interaction.reply(app.pipe.jobs[0]["ev"])
        self.assertEqual(text, revised)
        self.assertEqual(app.brain.openai.ask.await_count, 2)
        self.assertIn(previous, app.brain.openai.ask.await_args.args[2])
        self.assertIn("换一个开头和内容角度", app.brain.openai.ask.await_args.args[2])

    async def test_stubborn_repetition_falls_back_to_another_topic_and_rotates_style(self):
        app, now = self.active()
        app.brain.openai.ask.return_value = "你们喜欢稳一点，还是大胆试试新打法？"
        app.pipe.interaction.spoken(app.brain.openai.ask.return_value)
        prompts, spoken = [], []
        for _ in range(3):
            now[0] += 61
            app.pipe.interaction.tick()
            ev = app.pipe.jobs[0]["ev"]
            text, _, _ = await app.pipe.interaction.reply(ev)
            prompts.append(app.brain.openai.ask.await_args.args[2])
            spoken.append(text)
            app.pipe.interaction.spoken(text)
            settle(app)
        self.assertEqual(len(set(spoken)), 3)
        self.assertNotIn(app.brain.openai.ask.return_value, spoken)
        styles = [prompt.split("本次表达方式：")[1].split("\n")[0] for prompt in prompts]
        self.assertEqual(len(set(styles)), 3)


class FollowReminderTests(InteractionTestCase):
    def follow_app(self, *, due=True):
        app, now = make_app(follow=True)
        app.cfg["interaction"]["enabled"] = False
        app.pipe.interaction.sync_room()
        if due:
            now[0] += 181
        return app, now

    async def test_reminder_waits_three_minutes_and_live_room_can_be_quiet(self):
        app, now = self.follow_app(due=False)
        now[0] += 179
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        now[0] += 2
        for _ in range(10):
            app.pipe.interaction.tick()
        self.assertEqual(len(app.pipe.jobs), 1)
        self.assertTrue(app.pipe.jobs[0]["ev"]["follow_reminder"])
        self.assertFalse(app.pipe.interaction.viewers)

    async def test_interval_starts_when_spoken_and_does_not_repeat_early(self):
        app, now = self.follow_app()
        app.pipe.interaction.tick()
        ev = app.pipe.jobs[0]["ev"]
        self.assertIsNone(app.pipe.interaction.last_follow)
        now[0] += 25
        app.pipe.interaction.spoken(ev["text"], ev)
        settle(app)
        now[0] += 179
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        now[0] += 2
        app.pipe.interaction.tick()
        self.assertTrue(app.pipe.jobs[0]["ev"]["follow_reminder"])

    async def test_disabled_paused_disconnected_and_farewell_never_remind(self):
        for change in (lambda a: a.cfg["interaction"].update(follow_enabled=False),
                       lambda a: setattr(a.pipe, "paused", True),
                       lambda a: setattr(a.bili, "connected", False),
                       lambda a: setattr(a.schedule, "active_action", "farewell"),
                       lambda a: setattr(a.broadcast, "phase", "stopping"),
                       lambda a: setattr(a.broadcast, "phase", "idle")):
            app, _ = self.follow_app()
            change(app)
            app.pipe.interaction.tick()
            self.assertFalse(app.pipe.jobs)

    async def test_external_broadcast_requires_recent_activity_and_waits_for_viewer_reply(self):
        app, now = self.follow_app()
        app.broadcast.phase = "idle"
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        enter(app)
        app.pipe.interaction.tick()
        self.assertEqual([job["type"] for job in app.pipe.jobs], ["enter"])
        settle(app)
        now[0] += 19
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        now[0] += 2
        app.pipe.interaction.tick()
        self.assertTrue(app.pipe.jobs[0]["ev"]["follow_reminder"])

    async def test_ai_prompt_uses_chat_and_game_and_preserves_emotion(self):
        app, now = self.follow_app()
        app.cfg["live2d"]["enabled"] = True
        app.cfg["interaction"]["follow_ai_prompt"] = "顺着这局游戏亲切地求关注。"
        app.brain.openai.ask.return_value = "【开心】喜欢看我打牌的话，点个关注，下次一起玩呀！"
        app.dt = SimpleNamespace(running=True, commentary_context=lambda: "刚刚赢下战斗，生命12/60")
        app.pipe.interaction.dialogue.append({"at": now[0], "name": "小明", "text": "这把打得好", "reply": "谢谢夸奖"})
        app.pipe.interaction.tick()
        text, source, emotion = await app.pipe.interaction.reply(app.pipe.jobs[0]["ev"])
        self.assertIn("关注", text)
        self.assertEqual((source, emotion), ("关注点赞提醒", "开心"))
        model_cfg, _, prompt = app.brain.openai.ask.await_args.args
        for expected in ("顺着这局游戏亲切地求关注", "这把打得好", "12/60", "不要索要礼物"):
            self.assertIn(expected, prompt)
        self.assertLessEqual(model_cfg["timeout"], 8)
        self.assertEqual(app.cfg["ai"]["timeout"], DEFAULTS["ai"]["timeout"])

    async def test_failure_empty_missing_request_and_repetition_rotate_custom_fallbacks(self):
        app, now = self.follow_app()
        app.cfg["interaction"]["follow_templates"] = "喜欢就点个关注呀！\n开心的话点点赞吧！\n留个关注，下次一起玩！"
        results = [RuntimeError("模型离线"), "", "继续玩游戏吧", "留个关注，下次一起玩！"]
        spoken = []
        for result in results:
            app.brain.openai.ask.side_effect = result if isinstance(result, Exception) else None
            app.brain.openai.ask.return_value = result if isinstance(result, str) else ""
            app.pipe.interaction.tick()
            ev = app.pipe.jobs[0]["ev"]
            text, _, _ = await app.pipe.interaction.reply(ev)
            spoken.append(text)
            app.pipe.interaction.spoken(text, ev)
            settle(app)
            now[0] += 181
        self.assertEqual(spoken, ["喜欢就点个关注呀！", "开心的话点点赞吧！", "留个关注，下次一起玩！", "喜欢就点个关注呀！"])

    async def test_new_chat_during_generation_cancels_reminder(self):
        app, _ = self.follow_app()
        app.pipe.interaction.tick()
        ev = app.pipe.jobs[0]["ev"]
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args):
            started.set()
            await release.wait()
            return "喜欢就点个关注呀！"

        app.brain.openai.ask.side_effect = delayed
        request = asyncio.create_task(app.pipe.interaction.reply(ev))
        await started.wait()
        app.pipe.on_event({"type": "danmaku", "uid": 456, "name": "小红", "text": "主播你好"})
        release.set()
        self.assertEqual((await request)[0], "")
        self.assertIsNone(app.pipe.interaction.last_follow)

    async def test_disable_after_generation_drops_before_playback(self):
        app, _ = self.follow_app()
        app.pipe.interaction.tick()
        await self.workers(app, app.pipe._gen_worker)
        item = await asyncio.wait_for(app.pipe.synth_q.get(), 1)
        app.cfg["interaction"]["follow_enabled"] = False
        await app.pipe.play_q.put(item)
        await self.workers(app, app.pipe._play_worker)
        await wait_until(lambda: app.pipe.spoken_done(item["id"]))
        app.player.play_blocking.assert_not_called()
        self.assertIsNone(app.pipe.interaction.last_follow)

    async def test_reminder_is_spoken_before_narration_using_real_workers(self):
        app, now = self.follow_app()
        app.brain.openai.ask.return_value = "看得开心的话，点个关注，下次一起玩呀！"
        app.pipe.narrate("接着打下一张牌")
        app.pipe.interaction.tick()
        await self.workers(app, app.pipe._gen_worker, app.pipe._synth_worker, app.pipe._play_worker)
        await wait_until(lambda: sum(kind == "speak_end" for kind, _ in app.pushes) == 2)
        spoken = [data["text"] for kind, data in app.pushes if kind == "speak"]
        self.assertEqual(spoken, [app.brain.openai.ask.return_value, "接着打下一张牌"])
        self.assertEqual(app.player.play_blocking.call_count, 2)
        self.assertEqual(app.pipe.interaction.last_follow, now[0])

    async def test_room_or_connection_change_restarts_timer_and_invalidates_old_reminder(self):
        for change in (lambda a: setattr(a.bili, "room_id", 84),
                       lambda a: setattr(a.bili, "client", object())):
            app, now = self.follow_app()
            app.pipe.interaction.tick()
            ev = app.pipe.jobs[0]["ev"]
            settle(app)
            change(app)
            app.pipe.interaction.tick()
            self.assertFalse(app.pipe.jobs)
            self.assertFalse(app.pipe.interaction.can_speak(ev))
            now[0] += 181
            app.pipe.interaction.tick()
            self.assertTrue(app.pipe.jobs[0]["ev"]["follow_reminder"])

    async def test_short_interval_is_clamped_and_blank_templates_use_defaults(self):
        app, now = self.follow_app(due=False)
        app.cfg["interaction"].update(follow_interval_seconds=0, follow_templates=" \n ", follow_ai_prompt="")
        now[0] += 59
        app.pipe.interaction.tick()
        self.assertFalse(app.pipe.jobs)
        now[0] += 2
        app.pipe.interaction.tick()
        self.assertIn(app.pipe.jobs[0]["ev"]["text"], FOLLOW_REMINDER_TEMPLATES.splitlines())

    def test_existing_configs_get_enabled_default_and_keep_custom_settings(self):
        cfg = merge(DEFAULTS, {"interaction": {"idle_seconds": 35}})
        self.assertTrue(cfg["interaction"]["follow_enabled"])
        self.assertEqual(cfg["interaction"]["follow_interval_seconds"], 180)
        self.assertEqual(cfg["interaction"]["idle_seconds"], 35)
        cfg = merge(DEFAULTS, {"interaction": {"follow_enabled": False, "follow_interval_seconds": 600}})
        self.assertFalse(cfg["interaction"]["follow_enabled"])
        self.assertEqual(cfg["interaction"]["follow_interval_seconds"], 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
