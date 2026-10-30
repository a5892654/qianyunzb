"""弹幕点歌离线回归：免费/礼物/关注限制、真实消息入口及异步队列上限。"""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bili import BiliLive, _Handler
from blivedm.models.web import DanmakuMessage, InteractWordV2Message
from config import DEFAULTS
from pipeline import Pipeline
from songs import SongManager


class SongRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SimpleNamespace(cfg=copy.deepcopy(DEFAULTS), pushes=[], dt=None,
                                   bili=SimpleNamespace(owner_uid=999, is_following=AsyncMock(return_value=True)),
                                   pipe=Mock(), player=Mock())
        self.app.player.music_progress.return_value = (0, 0)
        self.app.push = lambda kind, data: self.app.pushes.append((kind, data))
        self.songs = self.app.songs = SongManager(self.app)
        self.song = {"id": 1, "name": "晴天", "artist": "测试歌手", "dur": 200, "exact": True}
        self.songs.ne.find = AsyncMock(return_value=self.song)

    def request(self, text, uid=123):
        return self.songs.on_danmaku({"type": "danmaku", "text": text, "uid": uid, "name": "小明"})

    async def settle(self):
        async with asyncio.timeout(2):
            while self.songs.pending_requests:
                await asyncio.sleep(0.005)

    async def test_plus_fullwidth_colon_space_and_traditional_all_search_clean_query(self):
        for command in ("点歌+晴天", "点歌＋晴天", "点歌:晴天", "点歌：晴天", "点歌 晴天", " /點歌 + 晴天 ", "点歌晴天"):
            with self.subTest(command=command):
                self.songs.queue.clear()
                self.assertTrue(self.request(command))
                await self.settle()
                self.assertEqual(self.songs.ne.find.await_args.args, ("晴天", 480))
                self.assertEqual(self.songs.queue[0]["name"], "晴天")
                self.assertEqual(self.songs.credits["123"]["song"], 0)

    async def test_plus_in_song_name_is_preserved_and_empty_commands_only_explain_format(self):
        self.request("点歌+Love+Love 歌手")
        await self.settle()
        self.assertEqual(self.songs.ne.find.await_args.args[0], "Love+Love 歌手")
        self.songs.ne.find.reset_mock()
        for text in ("点歌", "点歌+", "点歌＋  ", "点歌: "):
            self.assertTrue(self.request(text))
        self.songs.ne.find.assert_not_awaited()
        self.assertFalse(self.songs.pending_requests)
        self.assertIn("点歌", self.app.pipe.say.call_args.args[0])

    async def test_gift_switch_blocks_without_credit_then_consumes_one_credit(self):
        self.app.cfg["songs"]["require_gift"] = True
        self.request("点歌+晴天")
        self.songs.ne.find.assert_not_awaited()
        self.assertIn("送", self.app.pipe.say.call_args.args[0])
        self.songs.on_gift({"uid": 123, "name": "小明", "gift": "小花花", "num": 1})
        self.request("点歌+晴天")
        await self.settle()
        self.assertEqual(len(self.songs.queue), 1)
        self.assertEqual(self.songs.credits["123"]["song"], 0)

    async def test_failed_paid_search_refunds_credit(self):
        self.app.cfg["songs"]["require_gift"] = True
        self.songs._rec("123", "小明")["song"] = 1
        self.songs.ne.find.side_effect = RuntimeError("搜索离线")
        self.request("点歌+晴天")
        await self.settle()
        self.assertFalse(self.songs.queue)
        self.assertEqual(self.songs.credits["123"]["song"], 1)

    async def test_follow_mode_blocks_nonfollowers_even_with_song_credit_and_limits_hints(self):
        self.app.cfg["songs"].update(require_follow=True, require_gift=True)
        self.app.bili.is_following.return_value = False
        self.songs._rec("123", "小明")["song"] = 2
        for _ in range(2):
            self.assertTrue(self.request("点歌+晴天"))
            await self.settle()
        self.songs.ne.find.assert_not_awaited()
        self.assertEqual(self.songs.credits["123"]["song"], 2)
        self.assertIn("关注", self.app.pipe.say.call_args.args[0])
        self.assertEqual(self.app.pipe.say.call_count, 1)
        self.assertFalse(self.songs.pending_requests)

    async def test_followers_can_request_repeatedly_without_spending_gift_credit(self):
        self.app.cfg["songs"].update(require_follow=True, require_gift=True)
        for _ in range(2):
            self.request("点歌+晴天")
            await self.settle()
        self.assertEqual(len(self.songs.queue), 2)
        self.assertEqual(self.songs.credits["123"]["song"], 0)
        self.assertEqual(self.app.bili.is_following.await_count, 2)

    async def test_follow_lookup_failure_is_distinct_and_does_not_spend_credit(self):
        self.app.cfg["songs"].update(require_follow=True, require_gift=True)
        self.songs._rec("123", "小明")["song"] = 1
        self.app.bili.is_following.side_effect = RuntimeError("请登录当前直播间的主播账号")
        self.request("点歌+晴天")
        await self.settle()
        self.assertIn("暂时无法确认", self.app.pipe.say.call_args.args[0])
        self.assertEqual(self.songs.credits["123"]["song"], 1)
        self.songs.ne.find.assert_not_awaited()
        self.assertTrue(any(kind == "log" and "主播账号" in data["msg"] for kind, data in self.app.pushes))

    async def test_turning_follow_mode_off_restores_gift_and_free_rules(self):
        self.app.cfg["songs"].update(require_follow=False, require_gift=True)
        self.request("点歌+晴天")
        self.songs.ne.find.assert_not_awaited()
        self.app.cfg["songs"]["require_gift"] = False
        self.request("点歌+晴天")
        await self.settle()
        self.assertEqual(len(self.songs.queue), 1)
        self.app.bili.is_following.assert_not_awaited()

    async def test_follow_verification_reserves_queue_capacity_and_respects_disable(self):
        release = asyncio.Event()

        async def delayed(uid):
            await release.wait()
            return True

        self.app.cfg["songs"].update(require_follow=True, max_per_user=1, max_queue=2)
        self.app.bili.is_following.side_effect = delayed
        for uid in (123, 123, 456, 789):
            self.request("点歌+晴天", uid)
        self.assertEqual(self.songs.pending_requests.total(), 2)
        await asyncio.sleep(0)
        self.assertEqual(self.app.bili.is_following.await_count, 2)
        self.app.cfg["songs"]["enabled"] = False
        release.set()
        await self.settle()
        self.songs.ne.find.assert_not_awaited()

    async def test_real_follow_event_unlocks_request_even_when_follow_replies_are_disabled(self):
        self.app.cfg["songs"].update(require_follow=True, require_gift=True)
        self.app.cfg["events"]["follow"]["enabled"] = False
        self.app.pipe = Pipeline(self.app)
        self.app.bili = BiliLive(self.app.pipe.on_event, Mock())
        self.app.bili.connected = True
        self.app.bili.room_id, self.app.bili.owner_uid = 42, 999
        handler = _Handler(self.app.bili._emit)
        client = SimpleNamespace(room_id=42)
        handler._on_interact_word_v2(client, InteractWordV2Message(uid=123, username="小明", msg_type=2))
        handler._on_danmaku(client, DanmakuMessage(uid=123, uname="小明", msg="点歌+晴天"))
        await self.settle()
        self.assertEqual(len(self.songs.queue), 1)
        self.assertEqual(self.songs.credits["123"]["song"], 0)

    async def test_pending_searches_count_towards_per_user_and_total_limits(self):
        release = asyncio.Event()

        async def delayed(*args):
            await release.wait()
            return self.song

        self.songs.ne.find.side_effect = delayed
        self.app.cfg["songs"].update(max_per_user=1, max_queue=2)
        for uid in (123, 123, 456, 789):
            self.request("点歌+晴天", uid=uid)
        self.assertEqual(self.songs.pending_requests.total(), 2)
        release.set()
        await self.settle()
        self.assertEqual(len(self.songs.queue), 2)
        self.assertEqual(self.songs.ne.find.await_count, 2)

    async def test_real_danmaku_reaches_song_queue_without_ai_or_gift(self):
        self.app.pipe = Pipeline(self.app)
        _Handler(self.app.pipe.on_event)._on_danmaku(None,
            DanmakuMessage(uid=123, uname="小明", msg="点歌+晴天"))
        await self.settle()
        self.assertEqual(len(self.songs.queue), 1)
        self.assertTrue(any(kind == "event" and data.get("tname") == "点歌" for kind, data in self.app.pushes))
        self.assertTrue(all(job["type"] == "manual" for job in self.app.pipe.jobs))

    async def test_autoplay_does_not_fill_request_limit_and_requests_stay_before_it(self):
        self.app.cfg["songs"].update(max_queue=2, max_per_user=1)
        self.songs.enqueue_selected([dict(self.song, id=i) for i in range(100, 150)], source="autoplay")
        for uid in (123, 123, 456, 789):
            self.request("点歌+晴天", uid)
        await self.settle()
        self.assertEqual(self.songs.ne.find.await_count, 2)
        self.assertEqual([s["key"] for s in self.songs.queue[:2]], ["123", "456"])
        self.assertTrue(all(s["source"] == "autoplay" for s in self.songs.queue[2:]))
        self.assertEqual(len(self.songs.queue), 52)
        self.assertEqual(self.songs.credits["123"]["song"], 0)

    async def test_request_fifo_pin_credits_and_positions_ignore_background(self):
        self.songs.enqueue_selected([dict(self.song, id=100)], source="autoplay")
        self.songs._rec("456", "观众乙")["pin"] = 1
        self.request("点歌+晴天", 123)
        await self.settle()
        self.assertIn("排在第1首", self.app.pipe.say.call_args.args[0])
        self.request("点歌+晴天", 456)
        await self.settle()
        self.request("点歌+晴天", 789)
        await self.settle()
        self.assertEqual([s["key"] for s in self.songs.queue], ["456", "123", "789", "__autoplay__"])
        self.assertTrue(self.songs.queue[0]["pinned"])
        self.assertEqual(self.songs.credits["456"]["pin"], 0)
        self.assertIn("排在第3首", self.app.pipe.say.call_args.args[0])
        background = self.songs.queue[-1]
        for action in ("pin", "unpin", "up"):
            self.songs.control(action, background["qid"])
        self.songs.control("down", self.songs.queue[-2]["qid"])
        self.assertEqual(self.songs.queue[-1]["key"], "__autoplay__")
        self.assertFalse(background["pinned"])

    def test_disabled_and_ordinary_chat_do_not_search(self):
        self.assertFalse(self.request("你好主播"))
        self.app.cfg["songs"]["enabled"] = False
        self.assertFalse(self.request("点歌+晴天"))
        self.songs.ne.find.assert_not_awaited()


class FollowLookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bili = BiliLive(Mock(), Mock())
        self.bili.room_id, self.bili.owner_uid, self.bili.login_uid = 42, 999, 999
        self.bili.connected = True
        self.bili._cookie = "SESSDATA=fake;DedeUserID=999"
        self.user = Mock()
        self.user.get_relation = AsyncMock()
        self.factory = patch("bili.bili_user.User", return_value=self.user).start()
        self.addCleanup(patch.stopall)

    async def test_old_fan_checks_viewer_to_host_direction_and_rechecks_after_cache_expires(self):
        self.user.get_relation.return_value = {"relation": {"attribute": 0}, "be_relation": {"attribute": 2}}
        self.assertTrue(await self.bili.is_following(123))
        self.factory.assert_called_once()
        self.assertEqual(self.factory.call_args.args, (123,))
        self.assertEqual(self.factory.call_args.kwargs["credential"].dedeuserid, "999")
        self.assertTrue(await self.bili.is_following(123))
        self.user.get_relation.assert_awaited_once()
        self.bili._follow_cache[("42", "123")] = 0
        self.user.get_relation.return_value = {"relation": {"attribute": 2}, "be_relation": {"attribute": 0}}
        self.assertFalse(await self.bili.is_following(123))

    async def test_mutual_and_special_follow_events_and_other_rooms(self):
        for kind in (2, 4, 5):
            uid = kind * 100
            _Handler(self.bili._emit)._on_interact_word_v2(SimpleNamespace(room_id=42),
                InteractWordV2Message(uid=uid, username="观众", msg_type=kind))
            self.assertTrue(await self.bili.is_following(uid))
        self.user.get_relation.assert_not_awaited()
        self.bili.room_id = 43
        self.user.get_relation.return_value = {"be_relation": {"attribute": 0}}
        self.assertFalse(await self.bili.is_following(200))

    async def test_invalid_login_uid_or_response_never_unlocks_old_fan(self):
        for uid, login in ((0, 999), (123, None), (123, 888)):
            self.bili.login_uid = login
            with self.assertRaises(RuntimeError):
                await self.bili.is_following(uid)
        self.user.get_relation.assert_not_awaited()
        self.bili.login_uid = 999
        for response in ({}, {"be_relation": {}}, {"be_relation": {"attribute": None}}):
            self.user.get_relation.return_value = response
            with self.assertRaisesRegex(RuntimeError, "查询失败"):
                await self.bili.is_following(123)
        self.user.get_relation.side_effect = RuntimeError("private upstream error")
        with self.assertRaisesRegex(RuntimeError, "查询失败") as raised:
            await self.bili.is_following(123)
        self.assertNotIn("private", str(raised.exception))

    async def test_room_change_during_lookup_is_rejected_and_stop_clears_cached_followers(self):
        async def changed():
            self.bili.room_id = 43
            return {"be_relation": {"attribute": 2}}

        self.user.get_relation.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, "连接已改变"):
            await self.bili.is_following(123)
        self.bili._emit({"type": "follow", "uid": 123, "name": "观众", "room_id": 43})
        await self.bili.stop()
        self.assertFalse(self.bili._follow_cache)
        with self.assertRaisesRegex(RuntimeError, "先连接"):
            await self.bili.is_following(123)

    async def test_new_follow_arriving_during_lookup_overrides_delayed_negative_response(self):
        async def followed():
            self.bili._emit({"type": "follow", "uid": 123, "name": "观众", "room_id": 42})
            return {"be_relation": {"attribute": 0}}

        self.user.get_relation.side_effect = followed
        self.assertTrue(await self.bili.is_following(123))


if __name__ == "__main__":
    unittest.main(verbosity=2)
