"""Offline integration checks for the climbing program."""

import asyncio
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from climb import BASE_Y, CARRY_POSES, CHAPTERS, Climb, DEFAULTS, LEDGES, ROUND_BREAK
from climb_contact import cup_contact
from climb_course import Ledge
from live_climb import ClimbScene
import config
import main


class ClimbTests(unittest.TestCase):
    def setUp(self):
        self.config = {**DEFAULTS, "boat_gift": "测试礼物"}
        self.game = Climb(lambda: self.config)

    def tick(self, seconds):
        for _ in range(round(seconds * 60)):
            self.game.advance(1 / 60)

    def test_auto_reaches_summit_and_restarts_without_input(self):
        self.assertTrue(self.game.snapshot()["auto"])
        seen = set()
        missed_by_chapter = [0] * len(CHAPTERS)
        for frame in range(8 * 11_000):
            previous_misses = self.game.misses
            self.game.advance(0.125)
            if self.game.misses > previous_misses:
                chapter = LEDGES[self.game.checkpoint].chapter
                missed_by_chapter[chapter] += self.game.misses - previous_misses
            if frame % 80 == 0:
                seen.add(self.game.snapshot()["chapter"])
            if self.game.finished:
                break
        result = self.game.snapshot()
        self.assertTrue(result["finished"])
        self.assertEqual(result["standing"], len(LEDGES) - 1)
        self.assertEqual(seen, set(range(len(CHAPTERS))))
        self.assertGreater(result["turns"], len(LEDGES) - 1)
        self.assertGreater(sum(missed_by_chapter[-3:]), sum(missed_by_chapter[:3]))
        self.assertGreaterEqual(result["fall"], 20)
        self.assertGreaterEqual(result["clock"], 3 * 3600)
        self.assertLess(result["clock"], 3 * 3600 + 120)
        self.tick(3)
        self.assertEqual(self.game.snapshot()["clock"], result["clock"])
        self.tick(ROUND_BREAK)
        new_round = self.game.snapshot()
        self.assertEqual(new_round["round"], 2)
        self.assertFalse(new_round["finished"])
        self.assertGreater(new_round["turns"], 0)

    def test_climbing_difficulty_increases_with_each_chapter(self):
        for lower, higher in zip(CHAPTERS, CHAPTERS[1:]):
            self.assertLess(higher.width[0], lower.width[0])
            self.assertLess(higher.width[1], lower.width[1])
            self.assertGreater(higher.rise[0], lower.rise[0])
            self.assertGreater(higher.slope, lower.slope)
            self.assertGreater(higher.wind + higher.gust, lower.wind + lower.gust)
        ice = next(ledge for ledge in LEDGES if ledge.chapter >= 8 and ledge.material == "snow")
        steady_left, steady_right = ice.landing_bounds(0)
        fast_left, fast_right = ice.landing_bounds(300)
        self.assertLess(fast_right - fast_left, steady_right - steady_left)

    def test_the_suction_cup_stays_fixed_as_the_grip_pivots(self):
        self.tick(5.9)
        start = self.game.snapshot()["rod"]
        self.tick(0.55)
        end = self.game.snapshot()["rod"]
        self.assertTrue(start[4] and end[4])
        self.assertEqual(start[2:4], end[2:4])
        self.assertNotEqual(start[:2], end[:2])
        for rod in (start, end):
            self.assertAlmostEqual((rod[0] - rod[2]) ** 2 + (rod[1] - rod[3]) ** 2,
                                   (start[0] - start[2]) ** 2 + (start[1] - start[3]) ** 2)

    def test_cup_sweeps_into_top_wall_and_overhang(self):
        rock = Ledge(100, 200, 160, 100)
        cases = [((160, 160), (160, 230), (0, -1)),
                 ((40, 230), (220, 230), (-1, 0)),
                 ((180, 305), (180, 230), (0, 1))]
        for start, end, normal in cases:
            hit = cup_contact(start, end, nearby=(rock,))
            self.assertIsNotNone(hit)
            self.assertEqual((hit.nx, hit.ny), normal)
            self.assertIsNone(cup_contact(hit.point, start, nearby=(rock,)))

    def test_releasing_without_rod_contact_does_not_launch(self):
        self.game._launch((0.5, -0.866), 0.8, CARRY_POSES[0])
        self.assertEqual(self.game.snapshot()["turns"], 0)
        self.assertEqual(self.game.snapshot()["standing"], 0)

    def test_rod_hits_wall_before_the_basin_and_transfers_force(self):
        ledge = LEDGES[1]
        self.game.on_ledge = None
        self.game.x, self.game.y = ledge.x - 115, ledge.y + 78
        self.game.vx = 140
        self.game.flight_steps = 60
        self.game.release_angle = 0
        self.game.rod_length = 150
        self.game.carry_pose = (0, 1)
        self.game.cup_at = (ledge.x - 35, ledge.y + 37)
        self.game._air_rod()
        state = self.game.snapshot()
        self.assertEqual(state["rod_normal"], (-1, 0))
        self.assertLess(state["rod"][2], ledge.x)
        self.assertLess(state["vx"], 140)
        self.assertEqual(state["rod_hits"], 1)

    def test_edge_grab_holds_world_position_while_body_swings_up(self):
        ledge = LEDGES[1]
        self.game.on_ledge = None
        self.game.flight_target = ledge
        self.game.x, self.game.y = ledge.x - 66, ledge.y + 45
        self.game.flight_steps = 65
        self.game.cup_at = (ledge.x - 20, ledge.y - 20)
        self.game._boost(1, -0.5)
        first = self.game.snapshot()
        self.assertIsNone(first["standing"])
        self.assertTrue(first["rod"][4])
        self.tick(0.25)
        mid = self.game.snapshot()
        self.assertIsNone(mid["standing"])
        self.assertEqual(first["rod"][2:4], mid["rod"][2:4])
        self.assertNotEqual(first["y"], mid["y"])
        self.tick(0.4)
        self.assertEqual(self.game.snapshot()["standing"], 1)

    def test_pause_freezes_the_airborne_position(self):
        self.tick(2.1)
        self.assertIsNone(self.game.snapshot()["standing"])
        self.game.set_auto(False)
        position = self.game.snapshot()
        self.tick(2)
        self.assertEqual(self.game.snapshot(), position)
        self.game.set_auto(True)
        self.tick(1)
        self.assertGreater(self.game.snapshot()["record"], 0)

    def test_audience_cooldowns_and_distinct_viewers(self):
        self.assertTrue(self.game.on_event({"type": "danmaku", "text": "指路", "uid": 1}))
        self.assertIn("guidance", self.game.snapshot()["effects"])
        for uid in (1, 1, 2):
            self.game.on_event({"type": "danmaku", "text": "接住", "uid": uid})
        self.assertEqual(self.game.snapshot()["votes"]["接住"], (2, 3))
        self.game.on_event({"type": "danmaku", "text": "接住", "uid": 3})
        self.assertIn("net", self.game.snapshot()["effects"])
        for uid in (3, 4):
            self.game.on_event({"type": "danmaku", "text": "打气", "uid": uid})
        self.tick(2.0)
        self.assertEqual(self.game.snapshot()["jets"], 2)
        self.tick(16)
        self.assertNotIn("net", self.game.snapshot()["effects"])

    def test_paid_deduplicated_boat_recovers_last_rock(self):
        gift = {"type": "gift", "gift": "测试礼物", "paid": True, "gift_tid": "xy", "name": "观众甲"}
        self.assertFalse(self.game.on_event({**gift, "paid": False}))
        self.assertTrue(self.game.on_event(gift))
        self.assertFalse(self.game.on_event(gift))
        self.game.last_ledge = 2
        self.game.on_ledge = None
        self.game.x, self.game.y = 609, BASE_Y + 200
        self.tick(0.15)
        self.assertTrue(self.game.snapshot()["boat_riding"])
        self.tick(2.5)
        self.assertEqual(self.game.snapshot()["standing"], 2)
        self.assertNotIn("boat", self.game.snapshot()["effects"])
        self.assertFalse(self.game.on_event(gift))

    def test_step_creates_reachable_platform_on_launch(self):
        self.assertTrue(self.game.simulate("step", 0))
        self.assertNotIn("ledge", self.game.snapshot()["effects"]["step"])
        self.tick(2.3)
        ledge = self.game.snapshot()["effects"]["step"]["ledge"]
        self.assertGreater(ledge.y, LEDGES[1].y)
        self.assertLess(ledge.y, BASE_Y)
        self.tick(1.2)
        self.assertTrue(self.game.snapshot()["effects"]["step"]["used"])
        self.tick(10)
        self.assertGreater(self.game.snapshot()["record"], 0)


class RoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = self.app = object.__new__(main.App)
        app.cfg = copy.deepcopy(config.DEFAULTS)
        app.cfg["game"]["active"] = "climb"
        app.dt = app.dt_task = None
        app.climb = Climb(lambda: app.cfg["climb"])
        app.climb_scene = ClimbScene(app.climb, config.WEB_DIR)
        app.rice_gift = Mock()
        app.pipe = Mock(paused=False)
        app.push = Mock()
        app.game = Mock()
        app.broadcast = main.Broadcast(app)
        app.broadcast.capture.start_program = Mock()
        app.broadcast.prepare_overlay = AsyncMock()
        self.client = TestClient(TestServer(main.build_web(app)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_native_program_and_status(self):
        result = await self.client.post("/api/broadcast/preview", json={"audio_device": 0},
                                        headers={"X-BVB": "1"})
        self.assertTrue((await result.json())["program"])
        source, device = self.app.broadcast.capture.start_program.call_args.args
        self.assertEqual(device, 0)
        self.assertEqual(source().shape, (1080, 1920, 4))
        status = await (await self.client.get("/api/dtgame/status")).json()
        self.assertEqual(status["game_kind"], "climb")
        self.assertTrue(status["running"])

    async def test_controller_and_audience_share_one_scene(self):
        origin = str(self.client.make_url("/")).rstrip("/")
        socket = await self.client.ws_connect("/api/climb/control", origin=origin)
        stream = await self.client.get("/api/climb/stream.mjpg")
        try:
            self.assertIn(b"--frame", await stream.content.readuntil(b"\r\n"))
            self.app.on_event({"type": "danmaku", "uid": 3, "text": "指路"})
            self.assertIn("guidance", self.app.climb.snapshot()["effects"])
            for _ in range(45):
                state = self.app.climb.snapshot()
                if state["height"] > 0 and state["turns"] > 0:
                    break
                await asyncio.sleep(0.07)
            self.assertGreater(self.app.climb.snapshot()["height"], 0)
            self.assertEqual(self.app.climb.snapshot()["turns"], 1)
        finally:
            stream.close()
            await socket.close()

    async def test_duplicate_gift_mappings_are_rejected(self):
        with patch("config.save") as save:
            result = await self.client.post("/api/config", json={"climb": {"boat_gift": "同一种", "step_gift": "同一种"}},
                                            headers={"X-BVB": "1"})
        self.assertEqual(result.status, 400)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
