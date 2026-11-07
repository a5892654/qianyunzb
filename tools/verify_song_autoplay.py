# -*- coding: utf-8 -*-
"""开播自动播放歌单 · 离线验证：SongManager 触发 + 直播状态监听，不联网、不写真实配置。"""
import asyncio
import copy
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import config  # noqa: E402
from live_schedule import DailyBroadcastSchedule  # noqa: E402
from songs import SongManager  # noqa: E402


def song_app():
    app = SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), push=Mock(), pipe=Mock(), player=Mock())
    app.player.music_progress.return_value = (0, 0)
    app.songs = SongManager(app)
    return app


class SongNotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_autoplay_uses_remembered_playlist_once(self):
        app = song_app()
        app.cfg["songs"]["auto_playlist_ref"] = "456"
        manager = app.songs
        started = []

        async def play(reference):
            started.append(reference)
            manager.loop_playlist = {"id": reference, "name": "测试歌单", "ids": [1, 2], "version": 1}
            return {"id": reference, "name": "测试歌单", "total": 2, "loop": True}
        manager.play_playlist = AsyncMock(side_effect=play)

        self.assertTrue(manager.notify_live_started())
        await manager._auto_task
        self.assertEqual(started, ["456"])
        self.assertTrue(any("开播自动循环歌单" in call.args[1]["msg"] for call in app.push.call_args_list))
        self.assertFalse(manager.notify_live_started())      # 已经在循环，不重复
        await asyncio.sleep(0)
        self.assertEqual(started, ["456"])

    async def test_plan_playlist_override(self):
        app = song_app()
        manager = app.songs
        app.cfg["songs"]["auto_play_on_live"] = False
        started = []

        async def play(reference):
            started.append(reference)
            manager.loop_playlist = {"id": int(reference), "name": "计划歌单", "ids": [1], "version": 1}
            return {"id": reference, "name": "计划歌单", "total": 1, "loop": True}
        manager.play_playlist = AsyncMock(side_effect=play)

        self.assertTrue(manager.notify_live_started("999"))   # 计划歌单不依赖记忆与全局开关
        await manager._auto_task
        self.assertEqual(started, ["999"])
        self.assertFalse(manager.notify_live_started("999"))  # 同一歌单已在循环
        self.assertTrue(manager.notify_live_started("1000"))  # 下一段计划切换歌单
        await manager._auto_task
        self.assertEqual(started, ["999", "1000"])

    async def test_autoplay_skips_when_disabled_or_unset(self):
        app = song_app()
        manager = app.songs
        manager.play_playlist = AsyncMock()
        self.assertFalse(manager.notify_live_started())      # 还没记住歌单
        app.cfg["songs"]["auto_playlist_ref"] = "456"
        app.cfg["songs"]["auto_play_on_live"] = False
        self.assertFalse(manager.notify_live_started())      # 开关关闭
        app.cfg["songs"]["auto_play_on_live"] = True
        app.cfg["songs"]["enabled"] = False
        self.assertFalse(manager.notify_live_started())      # 点歌整体关闭
        manager.play_playlist.assert_not_awaited()

    async def test_autoplay_failure_logs_and_cools_down(self):
        app = song_app()
        app.cfg["songs"]["auto_playlist_ref"] = "456"
        manager = app.songs
        manager.play_playlist = AsyncMock(side_effect=RuntimeError("未登录"))
        self.assertTrue(manager.notify_live_started())
        await manager._auto_task
        self.assertTrue(any("开播自动播放歌单失败" in call.args[1]["msg"] for call in app.push.call_args_list))
        self.assertFalse(manager.notify_live_started())      # 冷却中不重试
        manager._auto_failed_at = time.monotonic() - manager.AUTO_RETRY_SECONDS - 1
        self.assertTrue(manager.notify_live_started())
        await manager._auto_task
        self.assertEqual(manager.play_playlist.await_count, 2)


def watch_app(*, source_open=True, running=False, auto_minutes=0, live=1, started_at=1000.0):
    songs = SimpleNamespace(cfg={"enabled": True, "auto_play_on_live": True, "auto_playlist_ref": "456"})
    broadcast = SimpleNamespace(running=running, source_open=source_open,
                                account=AsyncMock(return_value={"ok": True, "logged_in": True, "room_id": 42,
                                                                "live_status": live, "live_started_at": started_at}))
    schedule_cfg = {"enabled": False, "mode": "managed", "start_time": "08:00", "stop_time": "18:00",
                    "window_title": "", "audio_name": "", "last_start": "", "last_stop": "",
                    "plans": [], "plans_enabled": False, "last_starts": {}, "last_stops": {}}
    app = SimpleNamespace(cfg={"bili_cookie": "FAKE", "songs": songs.cfg,
                               "broadcast": {"schedule": schedule_cfg, "auto_stop_minutes": auto_minutes,
                                             "livehime": {}}},
                          songs=songs, broadcast=broadcast, notify_live_started=Mock(), push=Mock())
    now = [datetime(2026, 11, 1, 20, 0)]
    scheduler = DailyBroadcastSchedule(app, clock=lambda: now[0])
    return scheduler, app, now, broadcast


class LiveWatchTests(unittest.IsolatedAsyncioTestCase):
    async def _poll(self, scheduler):
        scheduler.refresh_music_watch()
        task = scheduler.music_check_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def test_detects_live_once_per_session(self):
        scheduler, app, now, _ = watch_app()
        await self._poll(scheduler)
        app.notify_live_started.assert_called_once()
        now[0] += timedelta(seconds=scheduler.MUSIC_CHECK_SECONDS + 1)
        await self._poll(scheduler)
        app.notify_live_started.assert_called_once()          # 同一场不重复触发

    async def test_new_live_triggers_again_after_offline(self):
        scheduler, app, now, broadcast = watch_app()
        await self._poll(scheduler)
        broadcast.account.return_value = {"ok": True, "logged_in": True, "room_id": 42,
                                          "live_status": 0, "live_started_at": 0}
        now[0] += timedelta(seconds=scheduler.MUSIC_CHECK_SECONDS + 1)
        await self._poll(scheduler)
        broadcast.account.return_value = {"ok": True, "logged_in": True, "room_id": 42,
                                          "live_status": 1, "live_started_at": 2000.0}
        now[0] += timedelta(seconds=scheduler.MUSIC_CHECK_SECONDS + 1)
        await self._poll(scheduler)
        self.assertEqual(app.notify_live_started.call_count, 2)

    async def test_no_poll_without_source_or_switch(self):
        for kwargs, mutate in (
            ({"source_open": False}, None),
            ({}, lambda app: app.songs.cfg.update(auto_play_on_live=False)),
            ({}, lambda app: app.songs.cfg.update(auto_playlist_ref="")),
            ({}, lambda app: app.songs.cfg.update(enabled=False)),
        ):
            scheduler, app, now, _ = watch_app(**kwargs)
            if mutate:
                mutate(app)
            scheduler.refresh_music_watch()
            self.assertIsNone(scheduler.music_check_task)
            app.notify_live_started.assert_not_called()

    async def test_auto_stop_monitor_owns_detection_when_enabled(self):
        scheduler, app, now, _ = watch_app(auto_minutes=3)
        scheduler.refresh_music_watch()
        self.assertIsNone(scheduler.music_check_task)          # 交给自动停播监测，避免重复请求

    async def test_app_started_stream_does_not_poll(self):
        scheduler, app, now, _ = watch_app(running=True)
        scheduler.refresh_music_watch()
        self.assertIsNone(scheduler.music_check_task)          # 手动开播已直接触发


class ScheduledStartMusicTests(unittest.IsolatedAsyncioTestCase):
    def build(self, plans, songs_cfg):
        from main import App

        broadcast = SimpleNamespace(
            running=False, phase="", source_open=False,
            capture=SimpleNamespace(previewing=False, hwnd=0, audio_device=0, error=""),
            sources=AsyncMock(return_value={
                "ok": True,
                "windows": [{"hwnd": 3333, "title": "DemonTower", "minimized": False}],
                "audio": [{"id": 2222, "name": "loop-x"}],
                "default_window": 3333, "default_audio": 2222}),
            preview=AsyncMock(return_value={"ok": True}),
            start=AsyncMock(return_value={"ok": True, "room_id": 42}),
            stop=AsyncMock(return_value={"ok": True}),
            account=AsyncMock(return_value={"ok": True, "logged_in": True, "room_id": 42,
                                            "live_status": 0}))
        app = SimpleNamespace(
            cfg={"bili_cookie": "FAKE", "audio": {}, "songs": songs_cfg,
                 "game": {"active": "demontower"},
                 "broadcast": {"title": "恶魔之塔 ZERO｜AI 游戏直播", "area_id": 283,
                               "auto_stop_minutes": 0, "livehime": {},
                               "schedule": {
                                   "enabled": True, "mode": "managed",
                                   "start_time": "08:00", "stop_time": "18:00",
                                   "window_title": "DemonTower", "audio_name": "loop-x",
                                   "last_start": "", "last_stop": "",
                                   "plans": plans, "plans_enabled": True,
                                   "last_starts": {}, "last_stops": {}}}},
            push=Mock(), pipe=Mock(), player=Mock(), broadcast=broadcast,
            livehime=Mock(), connect=AsyncMock(),
            ensure_game_and_dt=AsyncMock(return_value={"ok": True, "launched": False}),
            dt=SimpleNamespace(running=False), stop_dt=AsyncMock(),
            game=SimpleNamespace(sync=Mock()))
        app.player.music_progress.return_value = (0, 0)
        manager = SongManager(app)
        app.songs = manager
        app.notify_live_started = App.notify_live_started.__get__(app)
        started = []

        async def play(reference):
            started.append(reference)
            manager.loop_playlist = {"id": int(reference), "name": "zb", "ids": [1], "version": 1}
            return {"id": reference, "name": "zb", "total": 1, "loop": True}
        manager.play_playlist = AsyncMock(side_effect=play)

        now = [datetime(2026, 11, 6, 19, 59)]
        return DailyBroadcastSchedule(app, clock=lambda: now[0]), app, manager, started, now

    async def fire(self, scheduler, manager, now):
        with patch.object(config, "save", lambda cfg: None):
            now[0] = datetime(2026, 11, 6, 20, 0)
            scheduler.tick()
            while scheduler.jobs:
                await asyncio.gather(*list(scheduler.jobs), return_exceptions=True)
                await asyncio.sleep(0.01)
        await manager._auto_task

    async def test_timed_plan_start_plays_remembered_playlist(self):
        """到点自动开播成功后，App 桥接应真正拉起上次歌单循环。"""
        songs_cfg = copy.deepcopy(config.DEFAULTS["songs"])
        songs_cfg.update(enabled=True, auto_play_on_live=True, auto_playlist_ref="456")
        scheduler, app, manager, started, now = self.build(
            [{"id": "night", "start": "20:00", "stop": "23:00", "game": "demontower"}], songs_cfg)
        await self.fire(scheduler, manager, now)
        self.assertEqual(started, ["456"])
        self.assertEqual(manager.loop_playlist["id"], 456)
        self.assertTrue(any("开播自动循环歌单" in call.args[1]["msg"]
                            for call in app.push.call_args_list))

    async def test_timed_plan_start_plays_plan_playlist(self):
        """这一段计划指定了歌单时，即使没记住上次歌单也照常自动循环。"""
        songs_cfg = copy.deepcopy(config.DEFAULTS["songs"])
        songs_cfg.update(enabled=True, auto_play_on_live=False, auto_playlist_ref="")
        scheduler, app, manager, started, now = self.build(
            [{"id": "night", "start": "20:00", "stop": "23:00", "game": "demontower",
              "playlist": "999", "playlist_name": "夜间"}], songs_cfg)
        await self.fire(scheduler, manager, now)
        self.assertEqual(started, ["999"])
        self.assertEqual(manager.loop_playlist["id"], 999)


class ConfigAndUiTests(unittest.TestCase):
    def test_defaults_expose_autoplay_fields(self):
        cfg = config.DEFAULTS["songs"]
        self.assertIn("auto_play_on_live", cfg)
        self.assertIn("auto_playlist_ref", cfg)
        self.assertIn("auto_playlist_name", cfg)

    def test_ui_has_switch_and_remembers_playlist(self):
        html = (PROJECT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-k="songs.auto_play_on_live"', html)
        self.assertIn("每次开播自动循环播放上次的歌单", html)
        self.assertIn("auto_playlist_ref", html)
        self.assertIn("auto_playlist_name", html)
        self.assertIn("以后每次开播也会自动播放它", html)

    def test_plan_ui_has_playlist_field(self):
        html = (PROJECT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="plan-playlist"', html)
        self.assertIn("planPlaylistOptions", html)
        self.assertIn("playlist_name", html)
        self.assertIn("开播后自动循环的歌单", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
