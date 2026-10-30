"""离线验证直播白饭计数、AI胜利识别与采集合成；不运行或修改游戏。"""
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from aiohttp.test_utils import TestClient, TestServer

import config
from dtgame.player import Player
from dtgame.rice import RiceTracker
from live_rice import RiceCounter, RiceCompositor, layout_rect
from live_capture import WindowStreamCapture


def battle(act=3, run_id="run1", hp=30):
    return {"screen": "battle", "run": {"run_id": run_id, "act": act, "hp": hp},
            "battle": {"type": "boss", "hp": hp, "turn": 3,
                       "enemies": [{"id": "fallen_knight_boss", "name": "灰烬骑士", "hp": 90}]}}


def gift(**overrides):
    return {"type": "gift", "uid": 123, "room_id": 42, "gift": "小花花", "num": 1,
            "paid": True, "gift_tid": "send1", **overrides}


class RiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rice.json"
        self.counter = RiceCounter(self.path)
        self.enabled = True
        self.award = Mock()
        self.tracker = RiceTracker(self.counter, lambda: self.enabled, self.award)

    def arm(self, state=None):
        state = state or battle()
        self.tracker.action(state, {"action": "play"}, {"ok": True})

    def won(self, state=None):
        state = copy.deepcopy(state or battle())
        state["battle"].update(battle_over=True, awaiting_confirm=True)
        return state

    def test_ai_victory_once_and_restart_keeps_count_and_claim(self):
        self.arm()
        self.tracker.observe(self.won())
        self.assertEqual(self.counter.total, 1)
        self.assertEqual(self.award.call_count, 1)
        self.counter = RiceCounter(self.path)
        self.tracker = RiceTracker(self.counter, lambda: True)
        self.arm()
        self.tracker.observe(self.won())
        self.assertEqual(self.counter.total, 1)
        self.arm(battle(run_id="run2"))
        self.tracker.observe(self.won(battle(run_id="run2")))
        self.assertEqual(self.counter.total, 2)

    def test_manual_failed_ai_action_other_acts_and_loss_do_not_count(self):
        self.tracker.observe(self.won())
        self.tracker.action(battle(), {"action": "play"}, {"ok": False})
        self.tracker.observe(self.won())
        for act in (1, 2, 4):
            self.arm(battle(act=act))
            self.tracker.observe(self.won(battle(act=act)))
        self.arm()
        self.tracker.observe(self.won(battle(hp=0)))
        self.assertEqual(self.counter.total, 0)

    def test_off_and_stopping_ai_preserve_total_without_new_awards(self):
        self.arm()
        self.tracker.pause()
        self.tracker.observe(self.won())
        self.enabled = False
        self.arm()
        self.tracker.observe(self.won())
        self.assertEqual(self.counter.total, 0)
        self.enabled = True
        self.arm()
        self.tracker.observe(self.won())
        self.assertEqual(self.counter.total, 1)
        self.enabled = False
        self.assertEqual(self.counter.total, 1)

    def test_map_node_boss_context_handles_legacy_enemy_without_type(self):
        selected = {"screen": "map", "run": {"act": 3}, "map": {"choices": [{"col": 13, "row": 0, "type": "boss"}]}}
        self.tracker.action(selected, {"action": "node", "col": 13, "row": 0}, {"ok": True})
        current = {"screen": "battle", "run": {"act": 3, "hp": 20}, "battle": {"hp": 20, "enemies": [{"name": "新版Boss", "hp": 10}]}}
        self.arm(current)
        self.tracker.observe({"screen": "dialog", "run": current["run"], "dialog": {"title": "Boss魂 · 力量觉醒"}})
        self.assertEqual(self.counter.total, 1)

    def test_new_expedition_rotates_legacy_identity_and_preview_is_not_an_award(self):
        original = self.counter.session
        self.tracker.action({"screen": "hall"}, {"action": "expedition"}, {"ok": True})
        self.assertNotEqual(self.counter.session, original)
        self.counter.preview()
        self.assertEqual(self.counter.total, 0)
        self.assertTrue(self.counter.event_id)
        self.assertEqual(RiceCounter(self.path).total, 0)

    def test_ai_choosing_node_or_claiming_manual_boss_loot_is_not_combat(self):
        selected = {"screen": "map", "run": battle()["run"], "map": {"choices": [{"col": 13, "row": 0, "type": "boss"}]}}
        self.tracker.action(selected, {"action": "node", "col": 13, "row": 0}, {"ok": True})
        soul = {"screen": "dialog", "run": battle()["run"], "dialog": {"texts": ["Boss魂 · 力量觉醒"]}}
        self.tracker.observe(soul)
        self.tracker.action(soul, {"action": "overlay"}, {"ok": True})
        self.tracker.observe({"screen": "reward", "run": battle()["run"]})
        self.assertEqual(self.counter.total, 0)

    def test_loot_proves_victory_but_generic_map_does_not(self):
        self.arm()
        self.tracker.observe({"screen": "map", "run": battle()["run"], "map": {}})
        self.assertEqual(self.counter.total, 0)
        self.tracker.observe({"screen": "loot", "run": battle()["run"], "loot": {}})
        self.assertEqual(self.counter.total, 1)

    def test_save_failure_does_not_commit_count_or_dedupe(self):
        with patch("live_rice.os.replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError): self.counter.award("once")
        self.assertEqual(self.counter.total, 0)
        self.assertNotIn("once", self.counter.seen)

    def test_real_gifts_count_per_send_and_dedupe_after_restart(self):
        self.assertFalse(self.counter.record_gift(gift(num=10)))
        self.assertFalse(self.counter.record_gift(gift(num=10)))
        self.assertFalse(self.counter.record_gift(gift(paid=False, gift_tid="free1")))
        self.assertEqual((self.counter.total, self.counter.gift_progress), (0, 2))
        restarted = RiceCounter(self.path)
        self.assertFalse(restarted.record_gift(gift(num=10)))
        self.assertEqual((restarted.total, restarted.gift_progress), (0, 2))
        for i in range(8):
            self.assertEqual(restarted.record_gift(gift(gift_tid=f"small{i}")), i == 7)
        self.assertEqual((restarted.total, restarted.gift_progress), (1, 0))

    def test_gift_random_id_and_room_identity_are_respected(self):
        self.assertFalse(self.counter.record_gift(gift(gift_tid="", gift_rnd="random1")))
        self.assertFalse(self.counter.record_gift(gift(gift_tid="", gift_rnd="random1")))
        self.assertFalse(self.counter.record_gift(gift(gift_tid="", gift_rnd="random1", room_id=84)))
        self.assertEqual((self.counter.total, self.counter.gift_progress), (0, 2))

    def test_gifts_without_transaction_id_do_not_swallow_distinct_sends(self):
        first = gift(gift_tid="")
        self.assertFalse(self.counter.record_gift(first))
        self.assertFalse(self.counter.record_gift(first))
        self.assertFalse(self.counter.record_gift(gift(gift_tid="")))
        self.assertEqual((self.counter.total, self.counter.gift_progress), (0, 2))

    def test_simulated_empty_and_non_gift_events_do_not_count(self):
        for event in [gift(test=True), gift(type="danmaku"), *(gift(num=n) for n in (0, -1, None, "bad", True))]:
            self.assertFalse(self.counter.record_gift(event))
        self.assertEqual(self.counter.total, 0)

    def test_gifts_and_bosses_share_total_and_celebration(self):
        self.counter.record_gift(gift(value=10))
        event_id = self.counter.event_id
        self.arm()
        self.tracker.observe(self.won())
        self.assertEqual(self.counter.total, 2)
        self.assertNotEqual(self.counter.event_id, event_id)
        self.assertEqual(RiceCounter(self.path).total, 2)

    def test_ten_gift_events_award_once_and_keep_remainder(self):
        for i in range(23):
            gained = self.counter.record_gift(gift(num=100, gift_tid=f"gift{i}"))
            self.assertEqual(gained, (i + 1) % 10 == 0)
        self.assertEqual((self.counter.total, self.counter.gift_progress), (2, 3))
        restarted = RiceCounter(self.path)
        self.assertEqual((restarted.total, restarted.gift_progress), (2, 3))

    def test_large_gifts_award_once_and_preserve_ordinary_progress(self):
        self.assertFalse(self.counter.record_gift(gift(value=9.99, gift_tid="under")))
        self.assertTrue(self.counter.record_gift(gift(value=10, gift_tid="at")))
        self.assertEqual((self.counter.total, self.counter.gift_progress), (1, 1))
        self.assertTrue(self.counter.record_gift(gift(value=100, gift_tid="over")))
        self.assertFalse(self.counter.record_gift(gift(value=100, gift_tid="over")))
        self.assertFalse(self.counter.record_gift(gift(value=999, paid=False, gift_tid="free")))
        self.assertEqual((self.counter.total, self.counter.gift_progress), (2, 2))

    def test_large_gift_at_nine_does_not_double_award_or_reset_progress(self):
        for i in range(9): self.counter.record_gift(gift(gift_tid=f"small{i}"))
        self.assertEqual(self.counter.event_id, "")
        self.assertTrue(self.counter.record_gift(gift(value=10, gift_tid="large")))
        self.assertEqual((self.counter.total, self.counter.gift_progress), (1, 9))
        self.assertTrue(self.counter.record_gift(gift(gift_tid="tenth")))
        self.assertEqual((self.counter.total, self.counter.gift_progress), (2, 0))

    def test_progress_save_failure_is_atomic_and_can_retry(self):
        for i in range(9): self.counter.record_gift(gift(gift_tid=f"small{i}"))
        with patch("live_rice.os.replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError): self.counter.record_gift(gift(gift_tid="tenth"))
        self.assertEqual((self.counter.total, self.counter.gift_progress), (0, 9))
        self.assertTrue(self.counter.record_gift(gift(gift_tid="tenth")))
        self.assertEqual((self.counter.total, self.counter.gift_progress), (1, 0))

    def test_legacy_count_migrates_and_bosses_and_runs_preserve_gift_progress(self):
        self.path.write_text(json.dumps({"total": 12, "seen": ["old"], "session": "legacy"}), encoding="utf-8")
        self.counter = RiceCounter(self.path)
        self.assertEqual((self.counter.total, self.counter.gift_progress), (12, 0))
        self.counter.record_gift(gift())
        self.counter.award("boss")
        self.counter.begin_run()
        restarted = RiceCounter(self.path)
        self.assertEqual((restarted.total, restarted.gift_progress), (13, 1))
        self.assertIn("old", restarted.seen)

    def test_native_overlay_is_bottom_left_and_independent_of_live2d(self):
        self.counter.preview()
        settings = {"enabled": True}
        compositor = RiceCompositor(self.counter, lambda: settings)
        source = np.full((1080, 1920, 4), 80, np.uint8)
        capture = WindowStreamCapture()
        capture.rice_compositor = compositor
        self.assertIsNone(capture.compositor)
        capture._compose_frame(source)
        result = np.frombuffer(capture._frame, np.uint8).reshape(source.shape)
        self.assertTrue(np.any(result[900:1058, 22:395, :3] != 80))
        np.testing.assert_array_equal(result[:850], source[:850])
        np.testing.assert_array_equal(source, np.full_like(source, 80))
        with patch("live_rice.time.time", return_value=self.counter.celebrated_at + 0.3):
            a = source.copy(); compositor.apply(a)
        with patch("live_rice.time.time", return_value=self.counter.celebrated_at + 0.5):
            b = source.copy(); compositor.apply(b)
        self.assertTrue(np.any(a != b), "帧动画需要实际切换像素")
        settings["enabled"] = False
        capture._compose_frame(source)
        np.testing.assert_array_equal(np.frombuffer(capture._frame, np.uint8).reshape(source.shape), source)

    def test_rice_position_and_size_change_the_actual_composite(self):
        settings = {"enabled": True, "overlay_x": 0.5, "overlay_y": 0.25, "overlay_size": 0.25}
        compositor = RiceCompositor(self.counter, lambda: settings)
        for width, height in ((1920, 1080), (1280, 720), (320, 180)):
            for cx, cy in ((0.5, 0.25), (0, 1), (1, 0)):
                settings.update(overlay_x=cx, overlay_y=cy)
                frame = np.full((height, width, 4), 80, np.uint8)
                compositor.apply(frame)
                x, y, w, h = layout_rect(settings, width, height)
                changed = np.any(frame[..., :3] != 80, axis=2)
                self.assertTrue(changed[y:y+h, x:x+w].any())
                changed[y:y+h, x:x+w] = False
                self.assertFalse(changed.any(), "白饭必须完全画在指定位置，不残留原位置像素")
                self.assertEqual(h, round(height * 0.25))
        self.assertEqual(self.counter.total, 0)


class PlayerObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_reports_successful_ai_action_then_winning_state(self):
        bridge = Mock()
        bridge.screen.return_value = {"screen": "battle"}
        bridge.play_card.return_value = {"ok": True}
        bridge.call.return_value = {"ok": True}
        observer = Mock()
        player = Player({}, bridge, narrate=False, observer=observer)
        player._ask = AsyncMock(return_value={"action": "play", "slot": 0})
        await player.step(battle())
        self.assertEqual(observer.action.call_args.args[1], {"action": "play", "slot": 0})
        settled = battle(); settled["battle"]["awaiting_confirm"] = True
        await player.step(settled)
        self.assertEqual(observer.observe.call_count, 2)
        player.stop()
        observer.pause.assert_called_once()


class RiceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import main
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app = object.__new__(main.App)
        self.app.cfg = copy.deepcopy(config.DEFAULTS)
        self.app.dt = None
        self.app.push = Mock()
        self.app.rice = RiceCounter(Path(self.tmp.name) / "rice.json")
        self.app.broadcast = SimpleNamespace(capture=SimpleNamespace())
        self.app.bili = SimpleNamespace(client=None)
        self.client = TestClient(TestServer(main.build_web(self.app)))
        await self.client.start_server(); self.addAsyncCleanup(self.client.close)

    async def test_toggle_preview_and_transparent_browser_page(self):
        response = await self.client.post('/api/rice/preview', json={})
        self.assertEqual(response.status, 403)
        with patch('config.save'):
            response = await self.client.post('/api/config', json={"rice_counter": {"enabled": True}}, headers={"X-BVB": "1"})
        self.assertTrue((await response.json())["ok"])
        response = await self.client.post('/api/rice/preview', json={}, headers={"X-BVB": "1"})
        state = await response.json()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["total"], 0)
        self.assertTrue(state["event_id"])
        self.assertEqual((await self.client.get('/rice')).status, 200)
        self.assertEqual((await self.client.get('/static/assets/rice_bowl_celebrate.png')).status, 200)
        self.assertEqual(self.app.rice.total, 0)

    async def test_config_cannot_inject_real_count(self):
        response = await self.client.post('/api/config', json={"rice_counter": {"total": 999}}, headers={"X-BVB": "1"})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.app.rice.total, 0)

    async def test_layout_is_saved_and_exposed_to_browser_source(self):
        patch = {"overlay_x": 0.65, "overlay_y": 0.3, "overlay_size": 0.24}
        with unittest.mock.patch('config.save') as save:
            response = await self.client.post('/api/config', json={"rice_counter": patch}, headers={"X-BVB": "1"})
        self.assertEqual(response.status, 200)
        save.assert_called_once()
        state = await (await self.client.get('/api/rice/state')).json()
        self.assertEqual(state['layout'], {'x': 0.65, 'y': 0.3, 'size': 0.24})
        self.assertEqual(self.app.rice.total, 0)
        self.app.push.assert_called_with('rice', self.app.rice_state())
        for field, value in (("overlay_x", -0.1), ("overlay_y", 1.1), ("overlay_size", 0),
                             ("overlay_size", 0.7), ("overlay_size", None), ("overlay_x", True)):
            response = await self.client.post('/api/config', json={"rice_counter": {field: value}}, headers={"X-BVB": "1"})
            self.assertEqual(response.status, 400, (field, value))

    async def test_real_gift_packets_count_without_ai_or_voice_thanks(self):
        from bili import _Handler
        from blivedm.models.web import GiftMessage
        from pipeline import Pipeline
        self.app.cfg["rice_counter"]["enabled"] = True
        self.app.cfg["events"]["gift"].update(enabled=False, thank_free=False)
        self.app.bili.room_id = 42
        self.app.bili.owner_uid = 999
        self.app.songs = Mock()
        self.app.pipe = Pipeline(self.app, supporters=Mock())
        self.app.pipe.paused = True
        handler = _Handler(self.app.on_event)
        client = SimpleNamespace(room_id=42)
        paid = GiftMessage(uid=123, uname="小明", gift_name="小花花", num=10,
                           coin_type="gold", total_coin=5000, tid="paid1", rnd="rnd1")
        handler._on_gift(client, paid)
        handler._on_gift(client, paid)
        handler._on_gift(client, GiftMessage(uid=123, uname="小明", gift_name="小心心", num=1,
                                            coin_type="silver", tid="free1"))
        self.assertEqual((self.app.rice.total, self.app.rice.gift_progress), (0, 2))
        for i in range(8):
            handler._on_gift(client, GiftMessage(uid=123, uname="小明", gift_name="小心心", num=1,
                                                coin_type="silver", tid=f"free_more{i}"))
        handler._on_gift(client, GiftMessage(uid=123, uname="小明", gift_name="大礼物", num=1,
                                            coin_type="gold", total_coin=10000, tid="large1"))
        self.assertEqual(self.app.rice.total, 2)
        self.assertFalse(self.app.pipe.jobs)
        self.assertIsNone(self.app.dt)
        self.assertEqual(len([c for c in self.app.push.call_args_list if c.args[0] == "rice"]), 2)

    async def test_disabled_simulated_or_unsaved_gifts_do_not_break_event_flow(self):
        self.app.pipe = Mock()
        self.app.on_event(gift())
        self.assertEqual(self.app.rice.total, 0)
        self.app.cfg["rice_counter"]["enabled"] = True
        self.app.on_event(gift(test=True))
        self.assertEqual(self.app.rice.total, 0)
        with patch('live_rice.os.replace', side_effect=OSError("fixture")):
            self.app.on_event(gift())
        self.assertEqual(self.app.pipe.on_event.call_count, 3)
        self.assertEqual((self.app.rice.total, self.app.rice.gift_progress), (0, 0))

    async def test_large_gift_threshold_can_be_configured(self):
        self.app.pipe = Mock()
        with patch('config.save'):
            response = await self.client.post('/api/config', json={"rice_counter": {"enabled": True, "large_gift_yuan": 30}}, headers={"X-BVB": "1"})
        self.assertTrue((await response.json())["ok"])
        self.app.on_event(gift(value=10, gift_tid="small"))
        self.assertEqual((self.app.rice.total, self.app.rice.gift_progress), (0, 1))
        self.app.on_event(gift(value=30, gift_tid="large"))
        self.assertEqual((self.app.rice.total, self.app.rice.gift_progress), (1, 1))
        state = await (await self.client.get('/api/rice/state')).json()
        self.assertEqual(state["gift_progress"], 1)

    async def test_invalid_large_gift_threshold_is_rejected(self):
        for amount in (True, "10", 0, -1, float("nan"), float("inf")):
            response = await self.client.post('/api/config', json={"rice_counter": {"large_gift_yuan": amount}}, headers={"X-BVB": "1"})
            self.assertEqual(response.status, 400)
        self.assertEqual(self.app.cfg["rice_counter"]["large_gift_yuan"], 10)


if __name__ == '__main__': unittest.main(verbosity=2)
