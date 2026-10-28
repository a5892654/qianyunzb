"""离线心愿单动作回归：模拟 B站消息、AI 和音频设备，不读取真实配置。"""
import asyncio
import copy
import sys
import tempfile
import types
import unittest
import wave
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import main
from bili import _Handler
from pipeline import Pipeline
from tts import decode_audio_file
from wishlist import CHINA, WishlistActions

NOW = datetime(2026, 10, 29, 12, tzinfo=CHINA).timestamp()


def app_stub():
    app = types.SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), pushes=[], dt=None,
                                bili=types.SimpleNamespace(client=types.SimpleNamespace(room_id=42), room_id=42, owner_uid=1))
    app.cfg["room_id"] = 42
    app.cfg["wishlist"].update(enabled=True, action="say", text="{wish}达成，谢谢直播间{room}的大家！")
    app.push = lambda kind, data: app.pushes.append((kind, copy.deepcopy(data)))
    app.pipe = Mock()
    app.pipe.say.return_value = 1
    app.pipe.play_audio.return_value = 2
    app.brain = types.SimpleNamespace(ai=AsyncMock(return_value=("谢谢大家，我好开心！", "感谢")))
    app.songs = Mock()
    app.songs.ne.find = AsyncMock(return_value={"id": 1, "name": "测试歌曲", "artist": "测试歌手"})
    app.player = Mock()
    app.player.voice_clock.return_value = 0
    app.wishlist = WishlistActions(app, clock=lambda: NOW)
    return app


def event(**overrides):
    return {"type": "wishlist", "room_id": 42, "wish_id": "477", "wish_name": "今日心愿", "status": 2, **overrides}


def packet(status=2, cmd="WIDGET_WISH_LIST"):
    return {"cmd": cmd, "data": {"sid": 477, "wish_name": "今日心愿", "wish_status": status,
            "wish": [{"gift_name": "小花花", "target_num": 3, "current_num": 3}],
            "wish_status_info": [{"wish_status": 2, "wish_status_msg": "达成"}]}}


async def drain(manager):
    while manager.jobs:
        await asyncio.gather(*list(manager.jobs))


class WishlistTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = app_stub()
        self.saved = patch("wishlist.config.save")
        self.save = self.saved.start()
        self.addCleanup(self.saved.stop)
        self.addAsyncCleanup(self.app.wishlist.close)

    async def test_real_packet_pipeline_triggers_only_whole_list_completion(self):
        self.app.pipe = Pipeline(self.app)
        handler = _Handler(self.app.pipe.on_event)
        handler.handle(self.app.bili.client, packet(status=1))
        await drain(self.app.wishlist)
        self.assertEqual(self.app.pipe.jobs, [])
        handler.handle(self.app.bili.client, packet(cmd="WIDGET_WISH_LIST:1:2"))
        await drain(self.app.wishlist)
        self.assertEqual(len(self.app.pipe.jobs), 1)
        self.assertIn("今日心愿达成", self.app.pipe.jobs[0]["text"])
        self.assertIn("42", self.app.pipe.jobs[0]["text"])
        handler.handle(self.app.bili.client, packet(cmd="WIDGET_WISH_INFO"))
        await drain(self.app.wishlist)
        self.assertEqual(len(self.app.pipe.jobs), 1)
        self.save.assert_called_once()

    async def test_disabled_feature_still_shows_progress(self):
        self.app.cfg["wishlist"]["enabled"] = False
        self.app.wishlist.on_event(event())
        self.assertEqual(self.app.wishlist.state()["latest"]["status"], 2)
        self.app.pipe.say.assert_not_called()
        self.save.assert_not_called()

    async def test_repeated_push_and_restart_do_not_repeat_action(self):
        for _ in range(5):
            self.app.wishlist.on_event(event())
        await drain(self.app.wishlist)
        self.app.pipe.say.assert_called_once()
        restarted = WishlistActions(self.app, clock=lambda: NOW)
        restarted.on_event(event())
        await drain(restarted)
        self.app.pipe.say.assert_called_once()
        self.assertFalse(self.app.wishlist.state()["busy"])

    async def test_other_room_and_old_server_timestamp_do_not_trigger(self):
        self.app.wishlist.on_event(event(room_id=43))
        self.app.wishlist.on_event(event(wish_ts=NOW - 86400))
        self.app.wishlist.on_event(event(wish_ts="invalid"))
        await drain(self.app.wishlist)
        self.app.pipe.say.assert_not_called()
        self.save.assert_not_called()

    async def test_new_wishlist_and_new_day_can_trigger(self):
        self.app.wishlist.on_event(event())
        await drain(self.app.wishlist)
        self.app.wishlist.on_event(event(wish_id="478"))
        await drain(self.app.wishlist)
        self.app.wishlist.clock = lambda: NOW + 86400
        self.app.wishlist.on_event(event())
        await drain(self.app.wishlist)
        self.assertEqual(self.app.pipe.say.call_count, 3)

    async def test_ai_prompt_and_returned_emotion(self):
        self.app.cfg["wishlist"].update(action="ai", prompt="感谢大家完成{wish}，房间{room}")
        self.app.wishlist.on_event(event())
        await drain(self.app.wishlist)
        self.assertIn("今日心愿", self.app.brain.ai.await_args.args[1])
        self.app.pipe.say.assert_called_once_with("谢谢大家，我好开心！", "感谢")

    async def test_song_search_queues_reward_without_viewer_credit(self):
        self.app.cfg["wishlist"].update(action="song", song_query="测试歌曲 测试歌手")
        self.app.wishlist.on_event(event())
        await drain(self.app.wishlist)
        self.app.songs.ne.find.assert_awaited_once_with("测试歌曲 测试歌手", 480)
        self.assertEqual(self.app.songs.add.call_args.args[1], "心愿单奖励")
        self.app.pipe.say.assert_not_called()

    async def test_failed_action_can_be_tested_again_and_does_not_loop(self):
        self.app.cfg["wishlist"].update(action="song", song_query="测试歌曲")
        self.app.songs.ne.find.return_value = None
        self.app.wishlist.on_event(event())
        await drain(self.app.wishlist)
        self.assertFalse(self.app.wishlist.last_result["ok"])
        self.assertEqual(self.app.cfg["wishlist"]["_completed"], [])
        self.app.wishlist.on_event(event())
        await drain(self.app.wishlist)
        self.app.songs.ne.find.assert_awaited_once()
        self.app.songs.ne.find.return_value = {"id": 1, "name": "可播放的歌"}
        self.assertTrue((await self.app.wishlist.test())["ok"])
        self.assertEqual(self.app.cfg["wishlist"]["_completed"], [], "测试动作不能消耗正式达成机会")

    async def test_disabled_song_playback_and_missing_audio_have_clear_errors(self):
        self.app.cfg["wishlist"].update(action="song", song_query="测试歌曲")
        self.app.cfg["songs"]["enabled"] = False
        self.assertIn("启用点歌", (await self.app.wishlist.test())["error"])
        self.app.cfg["wishlist"].update(action="audio", audio_file="missing-file.mp3")
        self.assertIn("音频文件", (await self.app.wishlist.test())["error"])
        self.app.songs.ne.find.assert_not_awaited()

    async def test_persistence_failure_prevents_untracked_action(self):
        self.save.side_effect = OSError("磁盘不可写")
        self.app.wishlist.on_event(event())
        await drain(self.app.wishlist)
        self.app.pipe.say.assert_not_called()
        self.assertFalse(self.app.wishlist.last_result["ok"])
        self.assertEqual(self.app.cfg["wishlist"]["_completed"], [])

    async def test_malformed_packets_are_ignored(self):
        emitted = []
        handler = _Handler(emitted.append)
        for payload in (None, [], {}, {"sid": "bad", "wish_status": 2}, {"sid": 1, "wish_status": 99}):
            handler.handle(self.app.bili.client, {"cmd": "WIDGET_WISH_LIST", "data": payload})
        self.assertEqual(emitted, [])
        data = packet()
        data["data"]["wish"] = [None, {"target_num": "bad"}]
        handler.handle(self.app.bili.client, data)
        self.assertEqual(emitted[0]["items"], [])

    async def test_audio_decodes_and_uses_voice_pipeline_with_lipsync(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "logs") as directory:
            filename = Path(directory) / "AI-song.wav"
            with wave.open(str(filename), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes((np.sin(np.arange(2400) / 8) * 5000).astype("int16").tobytes())
            pcm, sr = decode_audio_file(filename)
            self.assertGreater(len(pcm), 0)
            self.assertGreater(sr, 0)
            self.app.pipe = Pipeline(self.app)
            self.app.engine = types.SimpleNamespace(synth=AsyncMock(side_effect=AssertionError("本地歌曲不应经过 TTS")))
            self.app.pipe._play_stream = AsyncMock(side_effect=AssertionError("本地歌曲不应进入流式 TTS"))
            self.app.cfg["wishlist"].update(action="audio", audio_file=str(filename))
            self.app.wishlist.on_event(event())
            await drain(self.app.wishlist)
            job = self.app.pipe.jobs[0]
            tasks = [asyncio.create_task(worker()) for worker in
                     (self.app.pipe._gen_worker, self.app.pipe._synth_worker, self.app.pipe._play_worker)]
            try:
                async def finished():
                    while not self.app.pipe.spoken_done(job["id"]):
                        await asyncio.sleep(0.001)
                await asyncio.wait_for(finished(), 3)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            self.app.player.play_blocking.assert_called_once()
            self.app.engine.synth.assert_not_awaited()
            speak = [data for kind, data in self.app.pushes if kind == "speak"][-1]
            self.assertTrue(speak["env"], "皮套应收到歌曲的口型包络")
            self.assertGreater(speak["dur"], 0)
            self.assertTrue(any(kind == "speak_end" for kind, _ in self.app.pushes))


class WishlistRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = app_stub()
        self.app.cfg["wishlist"]["_completed"] = ["private-completion-record"]
        self.saved = patch("main.config.save")
        self.save = self.saved.start()
        self.addCleanup(self.saved.stop)
        self.client = TestClient(TestServer(main.build_web(self.app)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.app.wishlist.close()

    async def test_state_and_manual_test_require_post_guard(self):
        denied = await self.client.post("/api/wishlist/test", json={})
        self.assertEqual(denied.status, 403)
        state = await (await self.client.get("/api/wishlist/state")).json()
        self.assertFalse(state["busy"])
        response = await self.client.post("/api/wishlist/test", json={}, headers={"X-BVB": "1"})
        self.assertTrue((await response.json())["ok"])
        self.app.pipe.say.assert_called_once()
        self.assertEqual(self.app.cfg["wishlist"]["_completed"], ["private-completion-record"])

    async def test_config_preserves_completion_ledger_and_rejects_bad_actions(self):
        response = await self.client.get("/api/config")
        self.assertNotIn("_completed", (await response.json())["wishlist"])
        headers = {"X-BVB": "1"}
        response = await self.client.post("/api/config", json={"wishlist": {"text": "新的台词", "_completed": []}}, headers=headers)
        self.assertTrue((await response.json())["ok"])
        self.assertEqual(self.app.cfg["wishlist"]["_completed"], ["private-completion-record"])
        self.assertEqual(self.app.cfg["wishlist"]["text"], "新的台词")
        response = await self.client.post("/api/config", json={"wishlist": {"action": "unknown"}}, headers=headers)
        self.assertEqual(response.status, 400)
        response = await self.client.post("/api/config", json={"wishlist": {"action": "audio", "audio_file": ""}}, headers=headers)
        self.assertEqual(response.status, 400)


class OfflineWishlistTests(unittest.TestCase):
    """无 socket / 无事件循环模式，检查解析、动作和队列各阶段。"""
    def setUp(self):
        self.app = app_stub()
        self.saved = patch("wishlist.config.save")
        self.save = self.saved.start()
        self.addCleanup(self.saved.stop)
        self.app.wishlist._spawn = Mock()

    @staticmethod
    def finish(coroutine):
        try:
            coroutine.send(None)
        except StopIteration as result:
            return result.value
        finally:
            coroutine.close()
        raise AssertionError("离线检查遇到未模拟的异步等待")

    def test_real_packets_route_only_whole_completion_and_deduplicate(self):
        self.app.pipe = Pipeline(self.app)
        handler = _Handler(self.app.pipe.on_event)
        handler.handle(self.app.bili.client, packet(status=1))
        self.app.wishlist._spawn.assert_not_called()
        handler.handle(self.app.bili.client, packet(cmd="WIDGET_WISH_LIST:1"))
        handler.handle(self.app.bili.client, packet(cmd="WIDGET_WISH_INFO"))
        self.app.wishlist._spawn.assert_called_once()
        ev, settings, key = self.app.wishlist._spawn.call_args.args
        result = self.finish(self.app.wishlist._perform(ev, settings))
        self.assertIn("播报队列", result)
        self.assertIn("今日心愿达成", self.app.pipe.jobs[0]["text"])
        self.assertIn("42", self.app.pipe.jobs[0]["text"])
        restarted = WishlistActions(self.app, clock=lambda: NOW)
        restarted._spawn = Mock()
        restarted.on_event(event())
        restarted._spawn.assert_not_called()

    def test_disabled_and_cross_room_events_and_old_days(self):
        self.app.cfg["wishlist"]["enabled"] = False
        self.app.wishlist.on_event(event())
        self.assertEqual(self.app.wishlist.state()["latest"]["status"], 2)
        self.app.cfg["wishlist"]["enabled"] = True
        for ev in (event(room_id=43), event(wish_ts=NOW - 86400), event(wish_ts="bad")):
            self.app.wishlist.on_event(ev)
        self.app.wishlist._spawn.assert_not_called()
        self.save.assert_not_called()

    def test_new_wish_and_day_are_new_completions(self):
        self.app.wishlist.on_event(event())
        self.app.wishlist.on_event(event(wish_id="478"))
        self.app.wishlist.clock = lambda: NOW + 86400
        self.app.wishlist.on_event(event())
        self.assertEqual(self.app.wishlist._spawn.call_count, 3)

    def test_ai_action_uses_prompt_and_generated_emotion(self):
        settings = {**self.app.wishlist.settings, "action": "ai", "prompt": "感谢大家完成{wish}，房间{room}"}
        self.finish(self.app.wishlist._perform(event(), settings))
        self.assertIn("今日心愿", self.app.brain.ai.await_args.args[1])
        self.app.pipe.say.assert_called_once_with("谢谢大家，我好开心！", "感谢")

    def test_song_action_searches_and_queues(self):
        settings = {**self.app.wishlist.settings, "action": "song", "song_query": "测试歌曲"}
        self.finish(self.app.wishlist._perform(event(), settings))
        self.app.songs.ne.find.assert_awaited_once_with("测试歌曲", 480)
        self.assertEqual(self.app.songs.add.call_args.args[1], "心愿单奖励")
        self.app.cfg["songs"]["enabled"] = False
        with self.assertRaisesRegex(RuntimeError, "启用点歌"):
            self.finish(self.app.wishlist._perform(event(), settings))

    def test_failed_action_reports_error_and_releases_completion(self):
        async def immediate(coroutine, timeout):
            return await coroutine
        self.app.wishlist._remember("completion-key")
        settings = {**self.app.wishlist.settings, "action": "song", "song_query": "测试歌曲"}
        self.app.songs.ne.find.return_value = None
        with patch("wishlist.asyncio.wait_for", immediate):
            result = self.finish(self.app.wishlist._run(event(), settings, "completion-key"))
        self.assertFalse(result["ok"])
        self.assertEqual(self.app.cfg["wishlist"]["_completed"], [])
        self.assertFalse(self.app.wishlist.state()["busy"])

    def test_persistence_failure_prevents_action(self):
        self.save.side_effect = OSError("磁盘不可写")
        self.app.wishlist.on_event(event())
        self.app.wishlist._spawn.assert_not_called()
        self.assertFalse(self.app.wishlist.last_result["ok"])
        self.assertEqual(self.app.cfg["wishlist"]["_completed"], [])

    def test_malformed_packets_and_missing_audio_are_rejected(self):
        emitted = []
        handler = _Handler(emitted.append)
        for data in (None, [], {}, {"sid": "bad", "wish_status": 2}):
            handler.handle(self.app.bili.client, {"cmd": "WIDGET_WISH_LIST", "data": data})
        self.assertEqual(emitted, [])
        settings = {**self.app.wishlist.settings, "action": "audio", "audio_file": "missing.mp3"}
        with self.assertRaisesRegex(ValueError, "音频文件"):
            self.finish(self.app.wishlist._perform(event(), settings))

    def test_local_audio_queue_decoding_and_lipsync_bypass_tts(self):
        class StopWorker(BaseException):
            pass
        async def immediate_thread(function, *args):
            return function(*args)
        async def immediate_executor(executor, function, *args):
            return function(*args)
        def clock_task(coroutine):
            coroutine.close()
            return Mock()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "logs") as directory:
            filename = Path(directory) / "AI-song.wav"
            with wave.open(str(filename), "wb") as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(24000)
                output.writeframes((np.sin(np.arange(2400) / 8) * 5000).astype("int16").tobytes())
            self.app.pipe = Pipeline(self.app)
            settings = {**self.app.wishlist.settings, "action": "audio", "audio_file": str(filename)}
            self.finish(self.app.wishlist._perform(event(), settings))
            job = self.app.pipe.jobs[0]
            self.app.pipe.has_job = Mock(wait=AsyncMock(side_effect=[True, StopWorker()]))
            with self.assertRaises(StopWorker):
                self.finish(self.app.pipe._gen_worker())
            item = self.app.pipe.synth_q.get_nowait()
            self.app.pipe.synth_q = Mock(get=AsyncMock(side_effect=[item, StopWorker()]))
            with patch("pipeline.asyncio.to_thread", immediate_thread), self.assertRaises(StopWorker):
                self.finish(self.app.pipe._synth_worker())
            decoded = self.app.pipe.play_q.get_nowait()
            self.assertGreater(len(decoded["pcm"]), 0)
            self.app.pipe.play_q = Mock(get=AsyncMock(side_effect=[decoded, StopWorker()]))
            self.app.pipe._play_stream = AsyncMock(side_effect=AssertionError("本地音频不能进入流式 TTS"))
            loop = types.SimpleNamespace(run_in_executor=immediate_executor)
            with patch("pipeline.asyncio.get_running_loop", return_value=loop), patch("pipeline.asyncio.create_task", clock_task):
                with self.assertRaises(StopWorker):
                    self.finish(self.app.pipe._play_worker())
            self.app.player.play_blocking.assert_called_once()
            self.app.pipe._play_stream.assert_not_awaited()
            speak = [data for kind, data in self.app.pushes if kind == "speak"][-1]
            self.assertTrue(speak["env"])
            self.assertGreater(speak["dur"], 0)
            self.assertTrue(self.app.pipe.spoken_done(job["id"]))

    def test_http_handlers_and_guard_without_network(self):
        app = main.build_web(self.app)
        def request(method, body=None, header=True):
            return types.SimpleNamespace(host="127.0.0.1", method=method,
                                         headers={"X-BVB": "1"} if header else {}, json=AsyncMock(return_value=body or {}))
        def handler(method, path):
            return next(route.handler for route in app.router.routes() if route.method == method and route.resource.canonical == path)
        test = handler("POST", "/api/wishlist/test")
        denied = self.finish(app.middlewares[0](request("POST", header=False), test))
        self.assertEqual(denied.status, 403)
        self.app.wishlist.test = AsyncMock(return_value={"ok": True, "message": "已加入队列"})
        result = self.finish(app.middlewares[0](request("POST"), test))
        self.assertEqual(result.status, 200)
        self.app.cfg["wishlist"]["_completed"] = ["protected-key"]
        get_config = handler("GET", "/api/config")
        self.assertNotIn('"_completed"', self.finish(get_config(request("GET"))).text)
        save = handler("POST", "/api/config")
        result = self.finish(save(request("POST", {"wishlist": {"text": "新台词", "_completed": []}})))
        self.assertEqual(result.status, 200)
        self.assertEqual(self.app.cfg["wishlist"]["_completed"], ["protected-key"])
        result = self.finish(save(request("POST", {"wishlist": {"action": "bad"}})))
        self.assertEqual(result.status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
