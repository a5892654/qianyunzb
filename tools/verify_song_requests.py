"""弹幕点歌离线回归：免费/礼物限制、加号格式及搜索中的队列上限。"""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bili import _Handler
from blivedm.models.web import DanmakuMessage
from config import DEFAULTS
from pipeline import Pipeline
from songs import SongManager


class SongRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SimpleNamespace(cfg=copy.deepcopy(DEFAULTS), pushes=[], dt=None,
                                   bili=SimpleNamespace(owner_uid=999), pipe=Mock(), player=Mock())
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

    def test_disabled_and_ordinary_chat_do_not_search(self):
        self.assertFalse(self.request("你好主播"))
        self.app.cfg["songs"]["enabled"] = False
        self.assertFalse(self.request("点歌+晴天"))
        self.songs.ne.find.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
