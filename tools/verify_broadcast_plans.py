# -*- coding: utf-8 -*-
"""播放计划（多段 开始-结束-游戏）离线验证：可控 clock + Mock Broadcast，不联网、不开播。"""
import asyncio
import copy
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import config

SAVED = []
config.save = lambda cfg: SAVED.append(json.loads(json.dumps(cfg)))

from live_schedule import DailyBroadcastSchedule  # noqa: E402


def clock_at(iso):
    return datetime.fromisoformat(iso)


def sources(*windows, audio=None, default_audio=2222, miss=False, ok=True, error=""):
    rows = [] if miss else [{"hwnd": h, "title": t, "minimized": False} for h, t in windows]
    devices = [{"id": 2222, "name": "loop-x"}] if audio is None else audio
    return {"ok": ok, "error": error, "windows": rows, "audio": devices,
            "default_window": rows[0]["hwnd"] if rows else 0,
            "default_audio": default_audio}


class Harness(unittest.IsolatedAsyncioTestCase):
    def make(self, iso="2026-10-01 19:59", *, plans=None, plans_enabled=True, mode="managed",
             settings=None):
        SAVED.clear()
        schedule_cfg = {
            "enabled": True, "mode": mode, "start_time": "08:00", "stop_time": "18:00",
            "window_title": "DemonTower", "audio_name": "loop-x", "last_start": "", "last_stop": "",
            "plans": plans or [], "plans_enabled": plans_enabled, "last_starts": {}, "last_stops": {},
        }
        if settings:
            schedule_cfg.update(settings)
        br = Mock(running=False)
        br.capture = types.SimpleNamespace(previewing=False, hwnd=0, audio_device=0, error="")
        br.sources = AsyncMock(return_value=sources((1111, "DemonTower")))
        br.preview = AsyncMock(return_value={"ok": True})
        br.stop_preview = AsyncMock(return_value={"ok": True})
        br.selection = Mock(return_value=(1111, 2222))
        br.start = AsyncMock(return_value={"ok": True, "room_id": 42})

        async def stop_now():
            br.running = False
            return {"ok": True}
        br.stop = AsyncMock(side_effect=stop_now)
        br.start_source = AsyncMock(return_value={"ok": True})
        br.stop_source = AsyncMock(return_value={"ok": True})
        br.stop_external = AsyncMock(return_value={"ok": True, "stopped": True, "room_id": 42})
        br.account = AsyncMock(return_value={"ok": True, "logged_in": True, "room_id": 42, "live_status": 0})
        lh = Mock(ensure_running=AsyncMock(return_value={"ok": True}),
                  trigger=Mock(return_value={"ok": True, "message": "已通知直播姬"}))
        app = types.SimpleNamespace(
            broadcast=br, livehime=lh, connect=AsyncMock(),
            pipe=Mock(say_job_id=Mock(return_value=1), spoken_done=Mock(return_value=True)),
            pushes=[], ensure_game_and_dt=AsyncMock(return_value={"ok": True, "launched": False}),
            dt=types.SimpleNamespace(running=False), stop_dt=AsyncMock(),
            game=types.SimpleNamespace(sync=Mock()),
            cfg={"bili_cookie": "FAKE", "room_id": 0, "audio": {},
                 "game": {"active": "demontower"},
                 "broadcast": {"title": "恶魔之塔 ZERO｜AI 游戏直播", "area_id": 283,
                               "auto_stop_minutes": 0, "schedule": schedule_cfg,
                               "livehime": {"path": "", "hotkey": "", "click_x": 0.0, "click_y": 0.0}}})
        app.push = lambda kind, data: app.pushes.append((kind, dict(data)))
        now = [clock_at(iso)]
        scheduler = DailyBroadcastSchedule(app, clock=lambda: now[0])
        scheduler.FAREWELL_TAIL = 0
        return scheduler, app, now

    async def drain(self, scheduler):
        while scheduler.jobs:
            await asyncio.gather(*list(scheduler.jobs), return_exceptions=True)
            await asyncio.sleep(0.01)

    async def fire(self, scheduler, now, iso):
        now[0] = clock_at(iso)
        scheduler.tick()
        await self.drain(scheduler)


class ValidationTests(Harness):
    def test_clean_plans_accepts_adjacent_and_cross_midnight(self):
        plans = DailyBroadcastSchedule._clean_plans([
            {"start": "20:00", "stop": "23:00", "game": "demontower"},
            {"start": "23:00", "stop": "01:00", "game": "stardew"},
            {"start": "01:00", "stop": "04:00", "game": "demontower"},
        ])
        self.assertEqual([p["game"] for p in plans], ["demontower", "stardew", "demontower"])
        self.assertTrue(all(p["id"] for p in plans))

    def test_clean_plans_rejects_bad_rows(self):
        cases = [
            ([{"start": "24:00", "stop": "01:00", "game": "demontower"}], "HH:MM"),
            ([{"start": "20:00", "stop": "20:00", "game": "demontower"}], "不能相同"),
            ([{"start": "20:00", "stop": "22:00", "game": "nope"}], "游戏"),
            ([{"start": "20:00", "stop": "23:00", "game": "demontower"},
              {"start": "22:00", "stop": "23:30", "game": "stardew"}], "重叠"),
            ([{"start": "23:00", "stop": "02:00", "game": "demontower"},
              {"start": "01:00", "stop": "03:00", "game": "stardew"}], "重叠"),
        ]
        for rows, want in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError) as ctx:
                DailyBroadcastSchedule._clean_plans(rows)
            self.assertIn(want, str(ctx.exception))

    async def test_save_plans_requires_login_and_plans(self):
        scheduler, app, _ = self.make()
        cases = [
            ({"enabled": True, "plans": []}, "至少添加"),
            ({"enabled": True, "plans": [{"start": "20:00", "stop": "23:00", "game": "demontower"}],
              "authorized": False}, "确认"),
        ]
        for payload, want in cases:
            with self.subTest(want=want), self.assertRaises(ValueError) as ctx:
                await scheduler.save_plans(payload)
            self.assertIn(want, str(ctx.exception))
        app.cfg["bili_cookie"] = ""
        with self.assertRaises(ValueError) as ctx:
            await scheduler.save_plans({"enabled": True, "authorized": True,
                                        "plans": [{"start": "20:00", "stop": "23:00", "game": "demontower"}]})
        self.assertIn("登录", str(ctx.exception))

    async def test_save_plans_persists_schedule_copy(self):
        scheduler, app, _ = self.make()
        payload = {"enabled": True, "authorized": True, "mode": "managed",
                   "plans": [{"id": "a", "start": "20:00", "stop": "23:00", "game": "demontower"},
                             {"id": "b", "start": "23:00", "stop": "01:00", "game": "stardew"}]}
        status = await scheduler.save_plans(payload)
        saved = SAVED[-1]["broadcast"]["schedule"]
        self.assertTrue(saved["enabled"] and saved["plans_enabled"])
        self.assertEqual(len(saved["plans"]), 2)
        self.assertEqual(status["plans"][1]["game"], "stardew")
        self.assertTrue(status["next_start"] and status["next_stop"])
        self.assertNotIn("hwnd", saved["plans"][0])

        app.broadcast.capture.previewing = True
        app.broadcast.capture.hwnd = 1111
        app.broadcast.capture.audio_device = 2222
        await scheduler.save({"enabled": True, "authorized": True, "title": "千音", "area_id": 591,
                              "start_time": "08:00", "stop_time": "18:00", "hwnd": 1111,
                              "audio_device": 2222})
        self.assertFalse(SAVED[-1]["broadcast"]["schedule"]["plans_enabled"])


class SchedulingTests(Harness):
    async def test_plan_start_uses_plan_game_and_auto_source(self):
        plans = [{"id": "night", "start": "20:00", "stop": "23:00", "game": "demontower"}]
        scheduler, app, now = self.make(plans=plans)
        await self.fire(scheduler, now, "2026-10-01 19:59")
        app.broadcast.preview.assert_not_awaited()

        app.broadcast.sources.return_value = sources((3333, "DemonTower"))
        await self.fire(scheduler, now, "2026-10-01 20:00")
        app.ensure_game_and_dt.assert_awaited_once()
        app.broadcast.preview.assert_awaited_once_with({"hwnd": 3333, "audio_device": 2222})
        start = app.broadcast.start.await_args.args[0]
        self.assertEqual((start["hwnd"], start["audio_device"], start["confirm"]), (3333, 2222, True))
        self.assertEqual(scheduler.settings["last_starts"]["night"], "2026-10-01")
        self.assertEqual(scheduler.active_plan_id, "night")
        self.assertEqual(app.cfg["game"]["active"], "demontower")

        app.broadcast.running = True
        app.broadcast.sources.return_value = sources((3333, "DemonTower"))
        await self.fire(scheduler, now, "2026-10-01 23:00")
        app.broadcast.stop.assert_awaited_once()
        self.assertEqual(scheduler.settings["last_stops"]["night"], "2026-10-01")

    async def test_plan_switches_game_and_window(self):
        plans = [{"id": "sv", "start": "20:00", "stop": "23:00", "game": "stardew"}]
        scheduler, app, now = self.make(plans=plans)
        app.dt.running = True
        app.broadcast.sources.return_value = sources(
            (1111, "DemonTower"), (4444, "Stardew Valley"))
        await self.fire(scheduler, now, "2026-10-01 20:00")
        app.stop_dt.assert_awaited_once()
        self.assertEqual(app.cfg["game"]["active"], "stardew")
        self.assertEqual(app.cfg["broadcast"]["title"], "星露谷物语｜AI 游戏直播")
        app.game.sync.assert_called_once()
        preview = app.broadcast.preview.await_args.args[0]
        self.assertEqual(preview, {"hwnd": 4444, "audio_device": 2222})
        self.assertIn("星露谷", app.pushes[-1][1]["msg"])

        scheduler2, app2, now2 = self.make(plans=[{"id": "dt", "start": "20:00", "stop": "23:00",
                                                    "game": "demontower"}])
        app2.broadcast.sources.return_value = sources((4444, "Stardew Valley"), (1111, "DemonTower"))
        await self.fire(scheduler2, now2, "2026-10-01 20:00")
        self.assertEqual(app2.broadcast.preview.await_args.args[0]["hwnd"], 1111)

    async def test_cross_midnight_and_next_day(self):
        plans = [{"id": "late", "start": "23:00", "stop": "02:00", "game": "demontower"}]
        scheduler, app, now = self.make(plans=plans)
        await self.fire(scheduler, now, "2026-10-01 23:00")
        app.broadcast.start.assert_awaited_once()
        app.broadcast.running = True
        await self.fire(scheduler, now, "2026-10-02 02:00")
        app.broadcast.stop.assert_awaited_once()
        self.assertEqual(scheduler.settings["last_stops"]["late"], "2026-10-02")
        app.broadcast.running = False
        await self.fire(scheduler, now, "2026-10-02 23:00")
        self.assertEqual(app.broadcast.start.await_count, 2)

    async def test_adjacent_plans_stop_then_start(self):
        plans = [{"id": "a", "start": "20:00", "stop": "22:00", "game": "demontower"},
                 {"id": "b", "start": "22:00", "stop": "23:00", "game": "stardew"}]
        scheduler, app, now = self.make(plans=plans)
        app.broadcast.running = True
        app.broadcast.sources.return_value = sources((4444, "Stardew Valley"))
        await self.fire(scheduler, now, "2026-10-01 22:00")
        app.broadcast.stop.assert_awaited_once()
        app.broadcast.start.assert_awaited_once()
        start = app.broadcast.start.await_args.args[0]
        self.assertEqual(start["hwnd"], 4444)
        self.assertEqual(app.cfg["game"]["active"], "stardew")
        self.assertEqual(scheduler.settings["last_starts"]["b"], "2026-10-01")
        self.assertEqual(scheduler.settings["last_stops"]["a"], "2026-10-01")

    async def test_plan_missing_window_never_starts(self):
        plans = [{"id": "sv", "start": "20:00", "stop": "23:00", "game": "stardew"}]
        scheduler, app, now = self.make(plans=plans)
        app.broadcast.sources.return_value = sources((1111, "DemonTower"))
        await self.fire(scheduler, now, "2026-10-01 20:00")
        app.broadcast.preview.assert_not_awaited()
        app.broadcast.start.assert_not_awaited()
        self.assertFalse(scheduler.last_result["ok"])
        self.assertIn("星露谷", scheduler.last_result["message"])

    async def test_plan_audio_falls_back_to_default(self):
        plans = [{"id": "a", "start": "20:00", "stop": "23:00", "game": "demontower"}]
        scheduler, app, now = self.make(plans=plans, settings={"audio_name": "已拔掉的设备"})
        app.broadcast.sources.return_value = sources(
            (1111, "DemonTower"), audio=[{"id": 7777, "name": "音箱"}], default_audio=7777)
        await self.fire(scheduler, now, "2026-10-01 20:00")
        self.assertEqual(app.broadcast.preview.await_args.args[0]["audio_device"], 7777)

    async def test_disabled_plans_do_nothing(self):
        plans = [{"id": "a", "start": "20:00", "stop": "23:00", "game": "demontower"}]
        scheduler, app, now = self.make(plans=plans, plans_enabled=False)
        await self.fire(scheduler, now, "2026-10-01 20:00")
        app.broadcast.start.assert_not_awaited()
        self.assertFalse(scheduler.plans_active)
        self.assertFalse(scheduler.status()["plans_enabled"])


class ApiTests(Harness):
    async def asyncSetUp(self):
        import main
        self.scheduler, self.app, _ = self.make(
            plans=[{"id": "a", "start": "20:00", "stop": "23:00", "game": "demontower"}])
        self.app.schedule = self.scheduler
        from aiohttp.test_utils import TestClient, TestServer
        self.client = TestClient(TestServer(main.build_web(self.app)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_plans_route_requires_header_and_validates(self):
        payload = {"enabled": True, "authorized": True, "mode": "managed",
                   "plans": [{"start": "20:00", "stop": "23:00", "game": "demontower"}]}
        response = await self.client.post("/api/broadcast/plans", json=payload)
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/broadcast/plans", json={**payload, "plans": []},
                                          headers={"X-BVB": "1"})
        self.assertEqual(response.status, 400)
        self.assertIn("至少添加", (await response.json())["error"])
        response = await self.client.post("/api/broadcast/plans", json=payload, headers={"X-BVB": "1"})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["ok"] and body["plans_enabled"])
        status = await (await self.client.get("/api/broadcast/schedule")).json()
        self.assertEqual(status["plans"][0]["game"], "demontower")
        self.assertTrue(status["next_start"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
