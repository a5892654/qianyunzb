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

    async def test_loop_metadata_contains_all_ids_without_loading_every_page(self):
        info = await self.ne.playlist_info("456")
        self.assertEqual(info["ids"], list(range(1, 206)))
        self.assertFalse(info["incomplete"])
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0][0].endswith("playlist/detail"))

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
        self.ne.playlist_info = AsyncMock(return_value={"id": 456, "name": "循环歌单", "ids": list(range(1, 206))})
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
        for body in ({"ids": []}, {"ids": [True]}, {"ids": [-1]}, {"ids": [1] * 101},
                     {"ids": [1], "mode": "invalid"}, {"ids": [1], "source": "invalid"}):
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

    async def test_batch_autoplay_keeps_viewer_requests_first_and_never_announces(self):
        self.app.songs.add(song(9), "观众", "viewer")
        self.app.pipe.say.reset_mock()
        result = await self.post("add", {"ids": [3, 2, 1], "mode": "play", "source": "autoplay"})
        self.assertTrue(result["ok"])
        self.assertEqual((result["added"], result["skipped"]), (2, 1))
        self.assertEqual([s["id"] for s in self.app.songs.queue], [9, 3, 1])
        self.assertEqual([s["source"] for s in self.app.songs.queue], ["request", "autoplay", "autoplay"])
        self.app.pipe.say.assert_not_called()
        self.app.player.stop_music.assert_not_called()

    async def test_single_manual_song_is_inserted_before_autoplay(self):
        self.app.songs.enqueue_selected([song(9), song(10)], source="autoplay")
        result = await self.post("add", {"ids": [1]})
        self.assertTrue(result["ok"])
        self.assertEqual([s["id"] for s in self.app.songs.queue], [1, 9, 10])
        self.assertEqual(self.app.songs.queue[0]["source"], "request")

    async def test_play_all_defaults_to_whole_playlist_loop_and_clear_stops_it(self):
        self.app.songs.add(song(9), "观众", "viewer")
        self.app.songs.enqueue_selected([song(77)], source="autoplay")
        result = await self.post("playlist/play", {"id": "456"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["playlist"], {"id": 456, "name": "循环歌单", "total": 205, "loop": True})
        self.ne.playlist_info.assert_awaited_with("456")
        self.assertEqual([s["id"] for s in self.app.songs.queue], [9])
        self.assertEqual(self.app.songs.state()["playlist"]["total"], 205)
        self.assertTrue((await self.post("control", {"action": "clear"}))["ok"])
        self.assertIsNone(self.app.songs.state()["playlist"])

    async def test_empty_partial_or_disabled_playlist_never_starts_loop(self):
        for info in ({"id": 456, "name": "空", "ids": []},
                     {"id": 456, "name": "部分", "ids": [1], "incomplete": True}):
            self.ne.playlist_info.return_value = info
            self.assertFalse((await self.post("playlist/play", {"id": "456"}))["ok"])
            self.assertIsNone(self.app.songs.loop_playlist)
        self.app.cfg["songs"]["enabled"] = False
        self.assertFalse((await self.post("playlist/play", {"id": "456"}))["ok"])


class PlaybackSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = app_stub()
        self.manager = self.app.songs
        self.manager.refresh_account = AsyncMock()
        self.manager.ne.url = AsyncMock(side_effect=lambda i: f"url-{i}")
        self.manager.ne.download = AsyncMock(side_effect=lambda url: url.encode())
        self.manager.ne.playlist_info = AsyncMock(return_value={"id": 456, "name": "循环歌单", "ids": [1, 3]})
        self.manager.ne.songs_by_ids = AsyncMock(side_effect=lambda ids: [dict(song(i), playable=True) for i in ids])
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

    async def test_autoplay_tracks_remain_silent_across_automatic_song_changes(self):
        self.manager.enqueue_selected([song(1), song(3)], source="autoplay", play_now=True)
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        self.app.pipe.say.assert_not_called()
        self.app.player.play_music.call_args.args[-1]()
        await self.wait_until(lambda: self.app.player.play_music.call_count == 2)
        self.assertEqual(self.manager.current["id"], 3)
        self.app.pipe.say.assert_not_called()

    async def test_viewer_requests_interrupt_background_in_fifo_then_return_to_silent_playlist(self):
        self.manager.enqueue_selected([song(1), song(3)], source="autoplay", play_now=True)
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        self.manager.ne.find = AsyncMock(return_value=song(9))
        self.manager.on_danmaku({"uid": 123, "name": "观众甲", "text": "点歌+歌曲9"})
        await self.wait_until(lambda: self.manager.current and self.manager.current["id"] == 9 and not self.manager.current["loading"])
        self.assertEqual([s["id"] for s in self.manager.queue], [1, 3])
        self.manager.ne.find.return_value = song(11)
        self.manager.on_danmaku({"uid": 456, "name": "观众乙", "text": "点歌+歌曲11"})
        await self.wait_until(lambda: not self.manager.pending_requests)
        self.assertEqual([s["id"] for s in self.manager.queue], [11, 1, 3])
        self.assertEqual(self.manager.current["id"], 9)
        self.app.player.play_music.call_args.args[-1]()
        await self.wait_until(lambda: self.manager.current and self.manager.current["id"] == 11 and not self.manager.current["loading"])
        self.app.player.play_music.call_args.args[-1]()
        await self.wait_until(lambda: self.manager.current and self.manager.current["id"] == 1 and not self.manager.current["loading"])
        speeches = [call.args[0] for call in self.app.pipe.say.call_args_list]
        self.assertEqual(len(speeches), 4, speeches)
        self.assertTrue(all("歌曲9" in text or "歌曲11" in text for text in speeches))
        self.assertEqual([s["id"] for s in self.manager.queue], [3])

    async def test_background_selection_does_not_interrupt_current_requested_song(self):
        self.manager.enqueue_selected([song(9)], play_now=True)
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        self.app.player.stop_music.reset_mock()
        self.app.pipe.say.reset_mock()
        self.manager.enqueue_selected([song(1), song(3)], source="autoplay", play_now=True)
        self.assertEqual(self.manager.current["id"], 9)
        self.app.player.stop_music.assert_not_called()
        self.app.pipe.say.assert_not_called()
        self.app.player.play_music.call_args.args[-1]()
        await self.wait_until(lambda: self.app.player.play_music.call_count == 2)
        self.assertEqual(self.manager.current["id"], 1)
        self.app.pipe.say.assert_not_called()

    async def test_request_during_background_loading_cancels_download_and_preserves_song(self):
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
        self.manager.enqueue_selected([song(1), song(3)], source="autoplay")
        await asyncio.wait_for(started.wait(), 3)
        self.manager.add(song(9), "观众甲", "123")
        self.manager.add(song(11), "观众乙", "456")
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.manager.current["id"], 9)
        self.assertEqual([s["id"] for s in self.manager.queue], [11, 1, 3])

    async def test_request_does_not_resume_manually_paused_background(self):
        self.manager.enqueue_selected([song(1), song(3)], source="autoplay")
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        self.manager.control("pause")
        self.manager.add(song(9), "观众甲", "123")
        await self.wait_until(lambda: self.manager.current is None)
        self.assertTrue(self.manager.paused)
        self.assertEqual([s["id"] for s in self.manager.queue], [9, 1, 3])
        self.assertEqual(self.app.player.play_music.call_count, 1)
        self.manager.control("resume")
        await self.wait_until(lambda: self.app.player.play_music.call_count == 2)
        self.assertEqual(self.manager.current["id"], 9)

    async def test_play_all_repeats_last_to_first_without_speaking(self):
        await self.manager.play_playlist("456")
        for count, expected in enumerate([1, 3, 1, 3, 1], 1):
            await self.wait_until(lambda: self.app.player.play_music.call_count == count)
            self.assertEqual(self.manager.current["id"], expected)
            if count < 5:
                self.app.player.play_music.call_args.args[-1]()
        self.assertTrue(self.manager.state()["playlist"]["loop"])
        self.app.pipe.say.assert_not_called()

    async def test_whole_playlist_passes_page_boundary_and_skips_unavailable_first_page(self):
        self.manager.ne.playlist_info.return_value["ids"] = list(range(1, 206))
        self.manager.ne.songs_by_ids.side_effect = lambda ids: [dict(song(i), playable=i in (101, 205)) for i in ids]
        await self.manager.play_playlist("456")
        for count, expected in enumerate([101, 205, 101], 1):
            await self.wait_until(lambda: self.app.player.play_music.call_count == count)
            self.assertEqual(self.manager.current["id"], expected)
            if count < 3:
                self.app.player.play_music.call_args.args[-1]()
        batches = [call.args[0] for call in self.manager.ne.songs_by_ids.await_args_list]
        self.assertEqual(batches[:3], [list(range(1, 101)), list(range(101, 201)), list(range(201, 206))])
        self.assertEqual(self.manager.state()["playlist"]["total"], 205)

    async def test_single_song_playlist_also_repeats(self):
        self.manager.ne.playlist_info.return_value["ids"] = [1]
        await self.manager.play_playlist("456")
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        first_qid = self.manager.current["qid"]
        self.app.player.play_music.call_args.args[-1]()
        await self.wait_until(lambda: self.app.player.play_music.call_count == 2)
        self.assertEqual(self.manager.current["id"], 1)
        self.assertNotEqual(self.manager.current["qid"], first_qid)

    async def test_viewer_request_runs_once_then_background_loop_continues(self):
        await self.manager.play_playlist("456")
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        self.manager.add(song(9), "观众", "viewer")
        await self.wait_until(lambda: self.app.player.play_music.call_count == 2)
        self.assertEqual(self.manager.current["id"], 9)
        for count, expected in enumerate([1, 3, 1], 3):
            self.app.player.play_music.call_args.args[-1]()
            await self.wait_until(lambda: self.app.player.play_music.call_count == count)
            self.assertEqual(self.manager.current["id"], expected)
        self.assertEqual(self.app.pipe.say.call_count, 2)
        self.assertTrue(self.manager.state()["playlist"]["loop"])

    async def test_request_can_interrupt_pending_playlist_metadata_loading(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def delayed_songs(ids):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.manager.ne.songs_by_ids.side_effect = delayed_songs
        await self.manager.play_playlist("456")
        await asyncio.wait_for(started.wait(), 3)
        self.manager.add(song(9), "观众", "viewer")
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.manager.current["id"], 9)
        self.assertEqual(self.manager.loop_playlist["cursor"], 0)

    async def test_new_playlist_replaces_previous_loop_and_keeps_requested_track(self):
        self.manager.enqueue_selected([song(9)], play_now=True)
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        await self.manager.play_playlist("456")
        self.manager.ne.playlist_info.return_value = {"id": 789, "name": "新歌单", "ids": [7, 8]}
        await self.manager.play_playlist("789")
        self.assertEqual(self.manager.current["id"], 9)
        self.app.player.play_music.call_args.args[-1]()
        for count, expected in enumerate([7, 8, 7], 2):
            await self.wait_until(lambda: self.app.player.play_music.call_count == count)
            self.assertEqual(self.manager.current["id"], expected)
            if count < 4:
                self.app.player.play_music.call_args.args[-1]()
        self.assertEqual(self.manager.state()["playlist"]["id"], 789)

    async def test_clear_stops_refill_and_does_not_resurrect_interrupted_background(self):
        await self.manager.play_playlist("456")
        await self.wait_until(lambda: self.app.player.play_music.call_count == 1)
        self.manager.control("clear")
        self.manager.add(song(9), "观众", "viewer")
        await self.wait_until(lambda: self.app.player.play_music.call_count == 2)
        self.assertEqual(self.manager.current["id"], 9)
        self.assertFalse(self.manager.queue)
        self.app.player.play_music.call_args.args[-1]()
        await self.wait_until(lambda: self.manager.current is None)
        self.assertIsNone(self.manager.loop_playlist)
        self.assertEqual(self.manager.ne.songs_by_ids.await_count, 1)

    async def test_no_playable_or_broken_audio_stops_loop_instead_of_retrying_forever(self):
        self.manager.ne.songs_by_ids.side_effect = lambda ids: [dict(song(i), playable=False) for i in ids]
        await self.manager.play_playlist("456")
        await self.wait_until(lambda: self.manager.loop_playlist is None)
        self.assertIn("暂无可完整播放", self.manager.last_error)
        self.app.player.play_music.assert_not_called()
        self.manager.ne.playlist_info.return_value["ids"] = [1]
        self.manager.ne.songs_by_ids.side_effect = lambda ids: [dict(song(i), playable=True) for i in ids]
        self.manager.ne.url.side_effect = None
        self.manager.ne.url.return_value = None
        await self.manager.play_playlist("456")
        await self.wait_until(lambda: self.manager.loop_playlist is None)
        self.assertEqual(self.manager.ne.url.await_count, 1)
        self.assertIn("暂无可完整播放", self.manager.last_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
