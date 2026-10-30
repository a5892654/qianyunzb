"""歌曲/歌单精确播放回归：本地网易云替身、真实 HTTP 路由及播放切换竞态。"""
import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import main
import netease
from songs import SongManager


def song(i):
    return {"id": i, "name": f"歌曲{i}", "artist": "歌手", "dur": 180}


def app_stub():
    app = SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), pipe=Mock(), player=Mock(), push=Mock())
    app.player.music_progress.return_value = (0, 0)
    app.player.decode_music.return_value = ([0], 44100)
    app.songs = SongManager(app)
    return app


class ReferenceTests(unittest.TestCase):
    def test_official_links_share_text_and_ids(self):
        for value, expected in (
            ("123", ("song", 123)),
            ("https://music.163.com/#/song?id=123&userid=9", ("song", 123)),
            ("分享歌曲 https://y.music.163.com/m/song?id=123&uct2=abc (@网易云音乐)", ("song", 123)),
            ("https://music.163.com/playlist?id=456", ("playlist", 456)),
        ):
            self.assertEqual(netease.music_reference(value), expected)
        self.assertIsNone(netease.music_reference("晴天 周杰伦"))
        self.assertEqual(netease.music_reference("456", "playlist"), ("playlist", 456))
        for value in ("https://other.example/song?id=1", "https://music.163.com/song?id=abc"):
            with self.assertRaises(ValueError):
                netease.music_reference(value)


class NetEaseLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.code = 200

        async def account(_):
            return web.json_response({"profile": {"userId": 42, "nickname": "测试账号"}, "account": {"id": 42}})

        async def post(request):
            form = await request.post()
            payload = json.loads(form["payload"])
            self.requests.append((request.path, payload, request.headers.get("Cookie", "")))
            if self.code != 200:
                return web.json_response({"code": self.code})
            if request.path.endswith("user/playlist"):
                return web.json_response({"code": 200, "playlist": [{"id": 456, "name": "我的收藏", "trackCount": 205}], "more": True})
            if request.path.endswith("playlist/detail"):
                return web.json_response({"code": 200, "playlist": {"name": "我的收藏", "trackCount": 205,
                    "trackIds": [{"id": i} for i in range(1, 206)], "tracks": [{"id": 1, "name": "仅返回的第一首"}]}})
            ids = [s["id"] for s in json.loads(payload["c"])]
            return web.json_response({"code": 200, "songs": [{"id": i, "name": f"歌曲{i}", "ar": [{"name": "歌手"}],
                "al": {"name": "专辑"}, "dt": 180000} for i in reversed(ids) if i != 4]})

        async def urls(request):
            ids = json.loads(request.query["ids"])
            return web.json_response({"code": 200, "data": [{"id": str(i), "url": f"https://audio.example/{i}" if i != 4 else None,
                "freeTrialInfo": {"start": 0, "end": 30} if i == 2 else None} for i in ids]})

        upstream = web.Application()
        upstream.router.add_get("/api/nuser/account/get", account)
        upstream.router.add_get("/api/song/enhance/player/url", urls)
        upstream.router.add_post("/weapi/{path:.*}", post)
        self.server = TestServer(upstream)
        await self.server.start_server()
        self.base = patch.object(netease, "BASE", str(self.server.make_url("/")).rstrip("/"))
        self.crypto = patch.object(netease, "_weapi_encrypt", side_effect=lambda data: {"payload": json.dumps(data)})
        self.base.start()
        self.crypto.start()
        self.ne = netease.NetEase(lambda: "MUSIC_U=fixture-token; __csrf=fixture-csrf")

    async def asyncTearDown(self):
        self.base.stop()
        self.crypto.stop()
        await self.server.close()

    async def test_account_uid_authenticated_playlists_and_csrf(self):
        result = await self.ne.playlists(100)
        self.assertEqual(result["playlists"][0]["id"], 456)
        self.assertTrue(result["more"])
        self.assertEqual(result["offset"], 101)
        _, payload, cookie = self.requests[-1]
        self.assertEqual(payload["uid"], 42)
        self.assertEqual(payload["offset"], 100)
        self.assertEqual(payload["csrf_token"], "fixture-csrf")
        self.assertIn("MUSIC_U=fixture-token", cookie)

    async def test_track_ids_recover_missing_tracks_in_order_and_paginate(self):
        first = await self.ne.playlist("456")
        self.assertEqual([s["id"] for s in first["songs"]], list(range(1, 101)))
        self.assertTrue(first["more"])
        self.assertFalse(first["songs"][1]["playable"], "试听不能标为完整播放")
        self.assertFalse(first["songs"][3]["playable"], "下架歌曲保留位置并标记不可播放")
        self.assertTrue(first["songs"][4]["playable"])
        last = await self.ne.playlist("https://music.163.com/#/playlist?id=456", 200)
        self.assertEqual([s["id"] for s in last["songs"]], list(range(201, 206)))
        self.assertFalse(last["more"])
        self.assertEqual(last["total"], 205)

    async def test_exact_song_id_never_searches_or_replaces_unavailable_song(self):
        self.ne.search = AsyncMock(side_effect=AssertionError("指定 ID 不应该搜索"))
        self.assertIsNone(await self.ne.find("2"))
        found = await self.ne.find("https://music.163.com/song?id=1")
        self.assertEqual(found["id"], 1)
        self.assertTrue(found["exact"])
        self.ne.search.assert_not_awaited()

    async def test_large_details_are_batched_and_upstream_errors_are_not_empty_lists(self):
        details = await self.ne.details(list(range(1, 206)))
        self.assertEqual(len(details), 204)
        self.assertEqual(len(self.requests), 3)
        self.code = 301
        with self.assertRaisesRegex(RuntimeError, "登录已失效"):
            await self.ne.playlist("456")
        anonymous = netease.NetEase(lambda: "")
        with self.assertRaisesRegex(RuntimeError, "请先登录"):
            await anonymous.playlists()


class SelectionRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = app_stub()
        self.ne = self.app.songs.ne
        self.ne.details = AsyncMock(return_value=[song(3), song(1), song(2)])
        self.ne.urls = AsyncMock(return_value={1: "url-1", 2: None, 3: "url-3"})
        self.ne.playlists = AsyncMock(return_value={"playlists": [], "more": False, "offset": 0})
        self.ne.playlist = AsyncMock(return_value={"songs": [], "name": "测试歌单"})
        self.client = TestClient(TestServer(main.build_web(self.app)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def post(self, path, body):
        response = await self.client.post("/api/songs/" + path, json=body, headers={"X-BVB": "1"})
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_selected_ids_preserve_order_skip_restricted_and_play_now_resumes(self):
        self.app.songs.add(song(9), "观众", "viewer")
        self.app.songs.paused = True
        result = await self.post("add", {"ids": [3, 2, 1], "mode": "play"})
        self.assertTrue(result["ok"])
        self.assertEqual((result["added"], result["skipped"]), (2, 1))
        self.assertEqual([s["id"] for s in self.app.songs.queue], [3, 1, 9])
        self.assertFalse(self.app.songs.paused)
        self.app.player.stop_music.assert_called_once()

    async def test_failures_never_enqueue_or_report_success(self):
        for body in ({"ids": []}, {"ids": [True]}, {"ids": [-1]}, {"ids": [1] * 101}, {"ids": [1], "mode": "invalid"}):
            self.assertFalse((await self.post("add", body))["ok"])
        self.assertFalse((await self.post("add", {"ids": [2]}))["ok"])
        self.assertFalse(self.app.songs.queue)
        self.app.cfg["songs"]["enabled"] = False
        self.assertIn("启用点歌", (await self.post("add", {"ids": [1]}))["error"])

    async def test_legacy_add_uses_server_details_and_queued_song_can_play_now(self):
        result = await self.post("add", {"song": {"id": 1, "name": "伪造元数据"}})
        self.assertTrue(result["ok"])
        self.assertEqual(self.app.songs.queue[0]["name"], "歌曲1")
        qid = self.app.songs.queue[0]["qid"]
        self.assertTrue((await self.post("control", {"action": "play", "qid": qid}))["ok"])

    async def test_playlist_routes_validate_and_forward_pagination(self):
        result = await (await self.client.get("/api/songs/playlists?offset=100")).json()
        self.assertTrue(result["ok"])
        self.ne.playlists.assert_awaited_with(100)
        self.assertTrue((await self.post("playlist", {"id": "456", "offset": 200}))["ok"])
        self.ne.playlist.assert_awaited_with("456", 200)
        self.assertFalse((await self.post("playlist", {"id": "456", "offset": "bad"}))["ok"])


class PlaybackSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = app_stub()
        self.manager = self.app.songs
        self.manager.refresh_account = AsyncMock()
        self.manager.ne.url = AsyncMock(side_effect=lambda i: f"url-{i}")
        self.manager.ne.download = AsyncMock(side_effect=lambda url: url.encode())
        self.task = asyncio.create_task(self.manager.run())

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def wait_until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.005)

    async def test_immediate_selection_cancels_old_download_and_starts_selected_id(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def download(url):
            if url == "url-1":
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return url.encode()

        self.manager.ne.download.side_effect = download
        self.manager.enqueue_selected([song(1)])
        await asyncio.wait_for(started.wait(), 3)
        self.manager.enqueue_selected([song(3)], play_now=True)
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.manager.current["id"], 3)
        self.assertEqual(self.app.player.decode_music.call_args.args[0], b"url-3")

    async def test_late_end_callback_cannot_end_next_song_and_pause_during_loading_applies(self):
        self.manager.enqueue_selected([song(1), song(3), song(5)])
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        old_callback = self.app.player.play_music.call_args.args[-1]
        self.manager.control("skip")
        await self.wait_until(lambda: self.app.player.play_music.call_count == 2)
        old_callback()
        await asyncio.sleep(0.03)
        self.assertEqual(self.manager.current["id"], 3)
        self.assertEqual(self.app.player.play_music.call_count, 2)
        self.manager.control("pause")
        self.assertTrue(self.manager.state()["paused"])
        self.manager.control("resume")
        self.assertFalse(self.manager.state()["paused"])

    async def test_playback_failure_is_visible_and_clears_stale_current(self):
        self.manager.ne.url.return_value = None
        self.manager.ne.url.side_effect = None
        self.manager.enqueue_selected([song(1)])
        await self.wait_until(lambda: bool(self.manager.last_error))
        self.assertIsNone(self.manager.state()["current"])
        self.assertIn("不能完整播放", self.manager.state()["error"])
        self.app.player.play_music.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
