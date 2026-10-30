"""直播姬停播离线回归：账号、平台、采集和时钟全部模拟，不会真实停播。"""
import asyncio
import copy
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import main
from live_broadcast import Broadcast
from live_platform import BiliBroadcastPlatform, PlatformError, _live_started_at
from live_schedule import DailyBroadcastSchedule


COOKIE = "SESSDATA=fixture; bili_jct=fixture-csrf"


class ExternalPlatformTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.platform = BiliBroadcastPlatform(COOKIE)
        self.platform.account = AsyncMock(return_value={"ok": True, "logged_in": True,
                                                       "room_id": 42, "uid": 9, "live_status": 1})
        self.room = SimpleNamespace(stop=AsyncMock(), get_room_info=AsyncMock(
            return_value={"room_info": {"live_status": 0}}))
        factory = patch("live_platform.LiveRoom", return_value=self.room)
        self.factory = factory.start()
        self.addCleanup(factory.stop)

    async def test_explicit_external_stop_checks_account_and_confirms_remote_offline(self):
        result = await self.platform.end_external(42)
        self.assertEqual(result, {"ok": True, "stopped": True, "room_id": 42})
        self.platform.account.assert_awaited_once()
        self.room.stop.assert_awaited_once()
        self.room.get_room_info.assert_awaited_once()
        self.assertEqual(self.platform.started_room_id, 0)

    async def test_default_end_still_does_not_stop_livehime_stream(self):
        self.assertFalse((await self.platform.end())["stopped"])
        self.room.stop.assert_not_awaited()
        self.platform.account.assert_not_awaited()

    async def test_wrong_room_or_login_never_sends_stop(self):
        for info in ({"logged_in": True, "room_id": 84, "live_status": 1},
                     {"logged_in": False, "room_id": 42, "live_status": 1}):
            self.platform.account.return_value = info
            with self.assertRaises(PlatformError):
                await self.platform.end_external(42)
        missing_csrf = BiliBroadcastPlatform("SESSDATA=fixture")
        with self.assertRaises(PlatformError):
            await missing_csrf.end_external(42)
        self.room.stop.assert_not_awaited()

    async def test_offline_room_is_noop_and_failed_request_is_not_success(self):
        self.platform.account.return_value["live_status"] = 0
        self.assertFalse((await self.platform.end_external(42))["stopped"])
        self.room.stop.assert_not_awaited()
        self.platform.account.return_value["live_status"] = 1
        self.room.stop.side_effect = RuntimeError("网络失败")
        with self.assertRaises(PlatformError):
            await self.platform.end_external(42)

    async def test_remote_still_live_is_reported_instead_of_claiming_success(self):
        self.room.get_room_info.return_value = {"room_info": {"live_status": 1}}
        with self.assertRaisesRegex(PlatformError, "仍显示直播中"):
            await self.platform.end_external(42)
        self.assertEqual(self.room.get_room_info.await_count, 3)

    async def test_broadcast_external_stop_routes_to_explicit_method_without_local_cleanup(self):
        broadcast = object.__new__(Broadcast)
        broadcast.app = SimpleNamespace(cfg={"bili_cookie": COOKIE})
        broadcast.lock = asyncio.Lock()
        fake = SimpleNamespace(end_external=AsyncMock(return_value={"ok": True, "stopped": True}))
        with patch("live_broadcast.BiliBroadcastPlatform", return_value=fake):
            self.assertTrue((await broadcast.stop_external(42))["stopped"])
        fake.end_external.assert_awaited_once_with(42)

    def test_platform_start_time_accepts_china_time_and_epoch(self):
        for value in (1704067200, "1704067200", "2024-01-01 08:00:00"):
            self.assertEqual(_live_started_at(value), 1704067200)
        for value in (None, "0000-00-00 00:00:00", "invalid", 0, float("nan"), float("inf")):
            self.assertEqual(_live_started_at(value), 0)


class ExternalScheduleTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = [datetime(2026, 10, 30, 19, 0)]
        self.app = SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), pushes=[], auto_stop_task=None)
        self.app.cfg["bili_cookie"] = COOKIE
        self.app.push = lambda kind, data: self.app.pushes.append((kind, data))
        self.app.broadcast = SimpleNamespace(
            running=False, phase="preview", account=AsyncMock(return_value={
                "ok": True, "logged_in": True, "room_id": 42, "uid": 9, "live_status": 1}),
            stop_external=AsyncMock(return_value={"ok": True, "stopped": True, "room_id": 42}),
            stop=AsyncMock(return_value={"ok": True}), start=AsyncMock(), sources=AsyncMock(),
            status=lambda: {"running": False, "phase": "preview"})
        self.app.pipe = Mock()
        self.app.pipe.say_job_id.return_value = 1
        self.app.pipe.spoken_done.return_value = True
        self.schedule = self.app.schedule = DailyBroadcastSchedule(self.app, clock=lambda: self.now[0])
        self.schedule.FAREWELL_TAIL = 0
        save = patch("config.save")
        self.save = save.start()
        self.addCleanup(save.stop)
        self.addAsyncCleanup(self.schedule.close)

    async def enable_stop_only(self):
        return await self.schedule.save({"enabled": True, "authorized": True, "mode": "stop_only",
                                         "stop_time": "23:00"})

    async def drain(self):
        await asyncio.gather(*list(self.schedule.jobs))


class ExternalScheduleTests(ExternalScheduleTestCase):
    async def test_stop_only_saves_without_preview_and_only_lists_next_stop(self):
        status = await self.enable_stop_only()
        self.assertNotIn("next_start", status)
        self.assertIn("next_stop", status)
        self.assertEqual(status["external_room_id"], 42)
        self.app.broadcast.sources.assert_not_awaited()
        self.save.assert_called_once()

    async def test_livehime_daily_stop_does_not_depend_on_local_running(self):
        await self.enable_stop_only()
        self.now[0] = datetime(2026, 10, 30, 20, 0)
        self.schedule.tick()
        await self.drain()
        self.app.broadcast.start.assert_not_awaited()
        self.now[0] = datetime(2026, 10, 30, 23, 0)
        self.schedule.tick()
        await self.drain()
        self.app.broadcast.stop_external.assert_awaited_once_with(42)
        self.app.pipe.say_job_id.assert_called_once_with(
            config.DEFAULTS["broadcast"]["auto_stop_line"], "", farewell=True)
        self.app.broadcast.stop.assert_not_awaited()
        self.assertTrue(self.schedule.last_result["ok"])
        self.schedule.tick()
        await self.drain()
        self.app.broadcast.stop_external.assert_awaited_once()

    async def test_explicit_daily_authorization_and_login_required_without_writing_settings(self):
        for patch_data in ({"authorized": False}, {"mode": "invalid"}):
            with self.assertRaises(ValueError):
                await self.schedule.save({"enabled": True, "authorized": True, "mode": "stop_only",
                                          "stop_time": "23:00", **patch_data})
        self.app.broadcast.account.return_value = {"ok": True, "logged_in": False}
        with self.assertRaises(ValueError):
            await self.enable_stop_only()
        self.save.assert_not_called()

    async def test_timer_finishes_farewell_before_external_stop_and_clears_state(self):
        order = []
        self.schedule.TIMER_SECONDS_PER_MINUTE = 0.01
        self.app.pipe.spoken_done.side_effect = lambda job: order.append("spoken") or True
        self.app.broadcast.stop_external.side_effect = lambda room: order.append("stop") or {"ok": True}
        status = await self.schedule.arm_external_timer(1)
        timer = self.schedule.external_timer_task
        self.assertTrue(status["external_timer"]["enabled"])
        self.assertEqual(status["external_timer"]["room_id"], 42)
        await asyncio.wait_for(timer, 2)
        self.assertEqual(order, ["spoken", "stop"])
        self.app.pipe.say_job_id.assert_called_once()
        self.assertFalse(self.schedule.status()["external_timer"]["enabled"])
        self.assertTrue(self.schedule.last_result["ok"])

    async def test_daily_stop_begins_farewell_early_without_stopping_early_or_repeating(self):
        await self.enable_stop_only()
        self.schedule.FAREWELL_LEAD_SECONDS = 0.3
        self.now[0] = datetime(2026, 10, 30, 22, 59, 59, 800000)
        self.schedule.tick()
        async with asyncio.timeout(2):
            while not self.app.pipe.say_job_id.called:
                await asyncio.sleep(0.005)
        self.app.broadcast.stop_external.assert_not_awaited()
        self.schedule.tick()
        await self.drain()
        self.now[0] = datetime(2026, 10, 30, 23, 0)
        self.schedule.tick()
        await self.drain()
        self.app.pipe.say_job_id.assert_called_once()
        self.app.broadcast.stop_external.assert_awaited_once_with(42)

    async def test_external_countdown_begins_farewell_before_stop_deadline(self):
        self.schedule.TIMER_SECONDS_PER_MINUTE = 0.3
        await self.schedule.arm_external_timer(1)
        timer = self.schedule.external_timer_task
        async with asyncio.timeout(2):
            while not self.app.pipe.say_job_id.called:
                await asyncio.sleep(0.005)
        self.app.broadcast.stop_external.assert_not_awaited()
        await asyncio.wait_for(timer, 2)
        self.app.broadcast.stop_external.assert_awaited_once_with(42)

    async def test_disabling_daily_plan_during_early_farewell_cancels_stop(self):
        await self.enable_stop_only()
        self.schedule.FAREWELL_LEAD_SECONDS = 0.3
        self.now[0] = datetime(2026, 10, 30, 22, 59, 59, 800000)
        self.schedule.tick()
        timer = self.schedule.daily_stop_task
        async with asyncio.timeout(2):
            while not self.app.pipe.say_job_id.called:
                await asyncio.sleep(0.005)
        await self.schedule.save({"enabled": False, "mode": "stop_only", "stop_time": "23:00"})
        await asyncio.gather(timer, return_exceptions=True)
        self.assertTrue(timer.cancelled())
        self.app.broadcast.stop_external.assert_not_awaited()
        self.assertEqual(self.schedule.settings["last_stop"], "")
        self.assertEqual(self.schedule.active_action, "")

    async def test_cancel_and_account_change_cancel_timer_without_stopping(self):
        for cancel in (self.schedule.cancel_external_timer, self.schedule.disable_for_account_change):
            await self.schedule.arm_external_timer(1)
            timer = self.schedule.external_timer_task
            cancel()
            await asyncio.gather(timer, return_exceptions=True)
            self.assertFalse(self.schedule.status()["external_timer"]["enabled"])
        self.app.broadcast.stop_external.assert_not_awaited()

    async def test_timer_refuses_offline_and_invalid_minutes_without_replacing_valid_timer(self):
        await self.schedule.arm_external_timer(1)
        original = self.schedule.external_timer_task
        for minutes in (0, -1, 1.5, True, "5", 10081):
            with self.assertRaises(ValueError):
                await self.schedule.arm_external_timer(minutes)
        self.app.broadcast.account.return_value["live_status"] = 0
        with self.assertRaisesRegex(ValueError, "尚未开播"):
            await self.schedule.arm_external_timer(1)
        self.assertIs(self.schedule.external_timer_task, original)

    async def test_cancel_during_account_lookup_does_not_arm_late(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def account():
            started.set()
            await release.wait()
            return {"ok": True, "logged_in": True, "room_id": 42, "live_status": 1}

        self.app.broadcast.account.side_effect = account
        task = asyncio.create_task(self.schedule.arm_external_timer(1))
        await started.wait()
        self.schedule.cancel_external_timer()
        release.set()
        with self.assertRaisesRegex(ValueError, "已取消"):
            await task
        self.assertIsNone(self.schedule.external_timer_task)

    async def test_account_changes_at_deadline_refuse_to_stop_wrong_room(self):
        self.schedule.TIMER_SECONDS_PER_MINUTE = 0.01
        await self.schedule.arm_external_timer(1)
        timer = self.schedule.external_timer_task
        self.app.broadcast.account.return_value["room_id"] = 84
        await asyncio.wait_for(timer, 2)
        self.app.broadcast.stop_external.assert_not_awaited()
        self.app.pipe.say_job_id.assert_not_called()
        self.assertFalse(self.schedule.last_result["ok"])

    async def test_automatic_stop_continues_when_farewell_times_out(self):
        self.schedule.TIMER_SECONDS_PER_MINUTE = 0.01
        self.schedule.FAREWELL_TIMEOUT = 0.01
        self.app.pipe.spoken_done.return_value = False
        await self.schedule.arm_external_timer(1)
        await asyncio.wait_for(self.schedule.external_timer_task, 2)
        self.app.broadcast.stop_external.assert_awaited_once_with(42)
        self.assertTrue(self.schedule.last_result["ok"])
        self.assertTrue(any(kind == "log" and "超时" in data.get("msg", "") for kind, data in self.app.pushes))

    async def test_automatic_stop_continues_when_farewell_cannot_be_queued(self):
        self.schedule.TIMER_SECONDS_PER_MINUTE = 0.01
        self.app.pipe.say_job_id.return_value = None
        await self.schedule.arm_external_timer(1)
        await asyncio.wait_for(self.schedule.external_timer_task, 2)
        self.app.broadcast.stop_external.assert_awaited_once_with(42)
        self.assertTrue(self.schedule.last_result["ok"])

    async def test_default_daily_stop_also_ends_livehime_room_without_local_output(self):
        self.schedule.settings.update(enabled=True, stop_time="23:00")
        self.now[0] = datetime(2026, 10, 30, 23, 0)
        self.schedule.tick()
        await self.drain()
        self.app.broadcast.stop_external.assert_awaited_once_with(42)
        self.app.broadcast.stop.assert_not_awaited()

    async def test_external_routes_require_header_and_accept_start_cancel_and_stop_only(self):
        client = TestClient(TestServer(main.build_web(self.app)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.post("/api/broadcast/timer/start", json={"minutes": 1})
        self.assertEqual(response.status, 403)
        self.app.broadcast.account.assert_not_awaited()
        headers = {"X-BVB": "1"}
        response = await client.post("/api/broadcast/timer/start", json={"minutes": 1}, headers=headers)
        self.assertTrue((await response.json())["external_timer"]["enabled"])
        response = await client.post("/api/broadcast/timer/cancel", json={}, headers=headers)
        self.assertFalse((await response.json())["external_timer"]["enabled"])
        response = await client.post("/api/broadcast/timer/start", json={"minutes": 0}, headers=headers)
        self.assertEqual(response.status, 400)
        response = await client.post("/api/broadcast/schedule", json={"enabled": True, "authorized": True,
            "mode": "stop_only", "stop_time": "23:00"}, headers=headers)
        self.assertEqual((await response.json())["mode"], "stop_only")


class AutomaticExternalStopTests(ExternalScheduleTestCase):
    async def probe(self, seconds=0):
        self.now[0] += timedelta(seconds=seconds)
        self.schedule.tick()
        if self.schedule.auto_check_task:
            await self.schedule.auto_check_task

    async def wait_for_stop(self):
        async with asyncio.timeout(2):
            while not self.app.broadcast.stop_external.await_count:
                await asyncio.sleep(0.005)

    async def test_saved_duration_waits_for_livehime_then_arms_automatically(self):
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 60
        self.app.broadcast.account.return_value["live_status"] = 0
        await self.probe()
        self.assertIsNone(self.schedule.external_timer_task)
        self.assertIn("等待", self.schedule.auto_message)
        self.app.broadcast.account.return_value["live_status"] = 1
        await self.probe(16)
        self.assertTrue(self.schedule.timer_automatic)
        self.assertEqual(self.schedule.timer_room_id, 42)
        self.assertEqual(self.schedule.timer_deadline, self.now[0] + timedelta(hours=1))
        timer = self.schedule.external_timer_task
        await self.probe(16)
        self.assertIs(self.schedule.external_timer_task, timer)

    async def test_timer_uses_actual_session_start_and_recalculates_changed_duration(self):
        start = self.now[0]
        self.app.broadcast.account.return_value["live_started_at"] = start.timestamp()
        self.now[0] += timedelta(minutes=10)
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 60
        await self.probe()
        self.assertEqual(self.schedule.timer_deadline, start + timedelta(hours=1))
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 20
        await self.probe()
        self.assertEqual(self.schedule.timer_deadline, start + timedelta(minutes=20))
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 0
        await self.probe()
        self.assertIsNone(self.schedule.external_timer_task)
        self.app.broadcast.stop_external.assert_not_awaited()

    async def test_restarting_during_overdue_stream_stops_room_without_button(self):
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 20
        self.app.broadcast.account.return_value["live_started_at"] = (self.now[0] - timedelta(minutes=30)).timestamp()
        await self.probe()
        await self.wait_for_stop()
        self.app.broadcast.stop_external.assert_awaited_once_with(42)
        self.app.broadcast.stop.assert_not_awaited()

    async def test_background_scheduler_detects_start_and_stops_without_any_web_request(self):
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 1
        self.schedule.TIMER_SECONDS_PER_MINUTE = 0.01
        self.schedule.start()
        await self.wait_for_stop()
        self.app.broadcast.stop_external.assert_awaited_once_with(42)
        await self.probe(16)
        self.app.broadcast.stop_external.assert_awaited_once()

    async def test_offline_then_new_session_gets_a_new_timer(self):
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 60
        await self.probe()
        original = self.schedule.external_timer_task
        self.app.broadcast.account.return_value["live_status"] = 0
        await self.probe(16)
        self.assertIsNone(self.schedule.external_timer_task)
        self.app.broadcast.account.return_value["live_status"] = 1
        await self.probe(16)
        self.assertIsNot(self.schedule.external_timer_task, original)
        self.assertEqual(self.schedule.timer_deadline, self.now[0] + timedelta(hours=1))

    async def test_transient_network_failure_retries_without_losing_armed_deadline(self):
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 60
        self.app.broadcast.account.side_effect = RuntimeError("暂时断网")
        await self.probe()
        self.assertIsNone(self.schedule.external_timer_task)
        self.app.broadcast.account.side_effect = None
        await self.probe(16)
        timer = self.schedule.external_timer_task
        self.app.broadcast.account.side_effect = RuntimeError("暂时断网")
        await self.probe(16)
        self.assertIs(self.schedule.external_timer_task, timer)

    async def test_local_output_and_disabled_duration_do_not_probe_external_account(self):
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 60
        self.app.broadcast.running = True
        await self.probe()
        self.app.broadcast.account.assert_not_awaited()
        self.app.broadcast.running = False
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 0
        await self.probe()
        self.app.broadcast.account.assert_not_awaited()

    async def test_api_saving_minutes_arms_automatically_and_disabling_clears_setting(self):
        client = TestClient(TestServer(main.build_web(self.app)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        headers = {"X-BVB": "1"}
        response = await client.post("/api/config", json={"broadcast": {"auto_stop_minutes": 60}}, headers=headers)
        self.assertTrue((await response.json())["ok"])
        await self.schedule.auto_check_task
        self.assertTrue(self.schedule.timer_automatic)
        response = await client.post("/api/broadcast/timer/cancel", json={}, headers=headers)
        status = await response.json()
        self.assertEqual(self.app.cfg["broadcast"]["auto_stop_minutes"], 0)
        self.assertFalse(status["external_timer"]["enabled"])
        self.assertEqual(status["external_timer"]["auto_minutes"], 0)
        self.app.broadcast.stop_external.assert_not_awaited()
        for minutes in (-1, True, 1.5, 10081):
            response = await client.post("/api/config", json={"broadcast": {"auto_stop_minutes": minutes}}, headers=headers)
            self.assertEqual(response.status, 400)
        self.assertEqual(self.app.cfg["broadcast"]["auto_stop_minutes"], 0)

    async def test_saving_zero_during_probe_cannot_arm_a_stale_timer(self):
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 60
        started, release = asyncio.Event(), asyncio.Event()

        async def account():
            started.set()
            await release.wait()
            return {"ok": True, "logged_in": True, "room_id": 42, "live_status": 1}

        self.app.broadcast.account.side_effect = account
        self.schedule.refresh_auto_timer()
        await started.wait()
        self.app.cfg["broadcast"]["auto_stop_minutes"] = 0
        self.schedule.refresh_auto_timer()
        release.set()
        await self.schedule.auto_check_task
        self.assertIsNone(self.schedule.external_timer_task)


if __name__ == "__main__":
    unittest.main(verbosity=2)
