"""Offline integration checks for the climbing program."""

import asyncio
import copy
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from climb import BASE_Y, CARRY_POSES, CHAPTERS, Climb, DEFAULTS, HEIGHT, LEDGES, ROUND_BREAK
from climb_contact import cup_contact
from climb_course import CHECKPOINTS, LANDMARKS, WATER_Y, Ledge
from climb_voice import ClimbPresenter
from climb_weather import WEATHERS
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

    def test_voice_describes_successive_jumps_before_launch(self):
        self.assertEqual(self.game.take_voice().kind, "opening")
        self.assertIsNone(self.game.take_voice())
        self.assertIsNone(self.game.take_operation_voice())
        self.tick(3)
        first = self.game.take_operation_voice()
        _, landing_x = self.game._target()
        self.assertIn("右上" if landing_x > self.game.x else "左上", first.fact)
        self.assertGreater(self.game.voice_remaining(first), 1.25)
        self.assertEqual(self.game.turns, 1)
        self.assertIsNone(self.game.take_operation_voice())
        self.tick(6.5)
        second = self.game.take_operation_voice()
        self.assertNotEqual(first.fact, second.fact)
        self.assertGreater(self.game.voice_remaining(second), 1.25)
        self.assertEqual(self.game.voice_remaining(first), 0)
        self.assertEqual(self.game.turns, 2)

    def test_forecast_waits_for_the_matching_landing_and_expires_on_reset(self):
        self.config["weather"] = False
        for _ in range(900):
            self.game.advance(0.125)
            if self.game.on_ledge == 20:
                break
        self.assertEqual(self.game.on_ledge, 20)
        plan = self.game.take_operation_voice()
        self.assertEqual(plan.at_ledge, 21)
        self.assertIsNone(self.game.voice_remaining(plan))
        for _ in range(60):
            self.game.advance(0.125)
            if self.game.on_ledge == plan.at_ledge:
                break
        self.assertGreater(self.game.voice_remaining(plan), 1.5)
        self.game.reset()
        self.assertEqual(self.game.voice_remaining(plan), 0)

    def test_audience_voice_and_reset(self):
        self.game.take_voice()
        self.assertTrue(self.game.simulate("指路", 0))
        self.assertIn("点亮了灯", self.game.take_voice().fact)
        self.game.set_auto(False)
        self.assertIsNone(self.game.take_voice())
        self.assertIsNone(self.game.take_operation_voice())
        self.game.reset()
        self.game.set_auto(True)
        self.assertEqual(self.game.take_voice().kind, "opening")

    def test_opening_slip_loses_real_height_and_climbing_recovers(self):
        self.game.take_voice()
        self.tick(10)
        self.assertIn("打滑风险", self.game.take_operation_voice().fact)
        self.tick(5)
        self.assertIn("吸盘", self.game.take_voice().fact)
        self.assertEqual(self.game.snapshot()["slips"], 1)
        self.tick(1.25)
        state = self.game.snapshot()
        self.assertEqual((state["standing"], state["record"], state["misses"]), (1, 2, 1))
        self.assertIn("5米", self.game.take_voice().fact)
        self.tick(12)
        state = self.game.snapshot()
        self.assertEqual((state["record"], state["slips"], state["comebacks"]), (3, 1, 1))
        self.assertIn("刷新了最高落脚点", self.game.take_voice().fact)

    def test_audience_guidance_prevents_the_opening_slip(self):
        self.tick(11)
        self.assertTrue(self.game.simulate("指路", 0))
        self.tick(6)
        self.assertEqual(self.game.snapshot()["record"], 3)
        self.assertEqual(self.game.snapshot()["slips"], 0)

    def test_auto_climbs_the_mid_route_without_stalling(self):
        self.assertTrue(self.game.snapshot()["auto"])
        target = CHECKPOINTS[7]
        seen, passed_landmarks = set(), set()
        worst_reposition = 0
        for frame in range(8 * 6 * 3600):
            self.game.advance(0.125)
            worst_reposition = max(worst_reposition, self.game.reposition_attempt)
            if LEDGES[self.game.last_ledge].landmark:
                passed_landmarks.add(self.game.last_ledge)
            if frame % 80 == 0:
                seen.add(self.game.snapshot()["chapter"])
            if self.game.finished or self.game.record_ledge >= target:
                break
        result = self.game.snapshot()
        self.assertGreaterEqual(result["record"], target)
        self.assertGreaterEqual(len(seen), 6)
        self.assertEqual(passed_landmarks, {index for site in LANDMARKS for index in range(site.first, site.last + 1)})
        self.assertGreater(result["turns"], target)
        self.assertGreaterEqual(result["fall"], 20)
        self.assertLessEqual(worst_reposition, 60)

    def test_finished_round_restarts_without_input(self):
        self.game.take_voice()
        self.game.finished = True
        self.game.result_left = ROUND_BREAK
        clock = self.game.run_clock
        self.tick(3)
        self.assertEqual(self.game.snapshot()["clock"], clock)
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

    def test_weather_rotates_after_the_intro_and_announces_its_effect(self):
        self.game.take_voice()
        for _ in range(60 * 150):
            self.game.advance(1 / 60)
            if self.game.snapshot()["weather"]["kind"] != "clear":
                break
        state = self.game.snapshot()
        self.assertTrue(state["weather"]["enabled"])
        self.assertNotEqual(state["weather"]["kind"], "clear")
        self.assertGreater(state["weather"]["since"], 0)
        cue = self.game.take_voice()
        self.assertIn(cue.fact, {weather.fact for weather in WEATHERS.values()})

    def test_weather_switch_changes_wind_and_disabled_weather_stays_clear(self):
        scenarios = {}
        for kind in ("head", "tail"):
            game = Climb(lambda: DEFAULTS)
            game.take_voice()
            game.set_weather(kind)
            first = game.snapshot()
            self.assertEqual(first["weather"]["prev"], "clear")
            self.assertLess(first["weather"]["mix"], 1)
            for _ in range(60 * 3):
                game.advance(1 / 60)
            scenarios[kind] = game.snapshot()
            self.assertEqual(scenarios[kind]["weather"]["mix"], 1)
        self.assertLess(scenarios["head"]["wind"] * scenarios["head"]["facing"], 0)
        self.assertGreater(scenarios["tail"]["wind"] * scenarios["tail"]["facing"], 0)
        self.assertEqual(scenarios["head"]["weather"]["kind"], "head")
        self.assertEqual(scenarios["tail"]["weather"]["kind"], "tail")
        self.config["weather"] = False
        self.game.set_weather("storm")
        state = self.game.snapshot()
        self.assertFalse(state["weather"]["enabled"])
        self.assertEqual(state["weather"]["kind"], "clear")

    def test_storm_adds_launch_slips(self):
        self.game.weather = replace(WEATHERS["storm"], slip=1.0)
        self.game.weather_until = 10 ** 9
        self.game.weather_until_clock = 10 ** 9
        while self.game.turns < 2:
            self.game.advance(1 / 60)
        self.assertGreater(self.game.snapshot()["slips"], 0)

    def test_fog_drifts_the_launch_sideways(self):
        clear, fog = Climb(lambda: DEFAULTS), Climb(lambda: DEFAULTS)
        for game in (clear, fog):
            game.take_voice()
            for _ in range(57):
                game.advance(1 / 60)
            self.assertIsNotNone(game.brace)
            game.flight_target = LEDGES[1]
        fog.set_weather("fog")
        shot = ((0.5, -0.866), 0.8, CARRY_POSES[0])
        clear._launch(*shot)
        fog._launch(*shot)
        self.assertNotEqual(fog.vx, clear.vx)

    def test_course_uses_distinct_transfer_lengths_and_ledge_shapes(self):
        profiles = {}
        for lower, upper in zip(LEDGES[4:], LEDGES[5:]):
            profiles.setdefault(upper.terrain, []).append((abs(upper.center - lower.center), upper.width))
        self.assertEqual(len(profiles), 5)
        tight = profiles["贴壁窄台"]
        cross = profiles["断崖横沟"]
        self.assertLess(sum(gap for gap, _ in tight) / len(tight) + 50,
                        sum(gap for gap, _ in cross) / len(cross))
        self.assertLess(sum(width for _, width in tight) / len(tight),
                        sum(width for _, width in profiles["长台转角"]) / len(profiles["长台转角"]))

    def test_remaining_landmark_surfaces_are_solid(self):
        for site in LANDMARKS:
            for index in range(site.first, site.last + 1):
                ledge = LEDGES[index]
                x, y = ledge.center, ledge.y
                hit = cup_contact((x, y - 45), (x, y + 12))
                self.assertIs(hit.ledge, ledge)
                self.assertLess(hit.ny, -0.9)
                below = ledge.underside(y)
                underside = cup_contact((x, below + 40), (x, below - 7))
                self.assertIs(underside.ledge, ledge)
                self.assertGreater(underside.ny, 0.9)

    def test_house_roof_and_lower_wall_are_solid(self):
        house = LEDGES[1]
        self.assertEqual(house.model, "weigh_house")
        y = house.y + 175
        self.assertLess(y, house.underside(house.y))
        roof = cup_contact((house.center, house.y - 32), (house.center, house.y + 8))
        self.assertIs(roof.ledge, house)
        self.assertLess(roof.ny, -0.99)
        wall = cup_contact((house.x - 32, y), (house.x + 8, y))
        self.assertIs(wall.ledge, house)
        self.assertLess(wall.nx, -0.99)
        self.game.x, self.game.y, self.game.vx = house.x + 12, y + 31, 180
        self.game._rock_impact(house.x - 12, self.game.y)
        self.assertLess(self.game.vx, 0)
        self.assertLess(self.game.x, house.x + 7)

    def test_new_follower_fires_a_short_nitro_burst(self):
        follow = {"type": "follow", "uid": 7, "name": "新观众小满", "room_id": 9}
        self.assertTrue(self.game.on_event(follow))
        state = self.game.snapshot()
        self.assertIsNone(state["standing"])
        self.assertGreater(state["nitro"], 0)
        self.assertEqual(state["turns"], 1)
        self.assertIn("新观众小满", state["notice"])
        self.assertIn("氮气", state["notice"])
        self.tick(0.35)
        self.assertLess(self.game.snapshot()["y"], BASE_Y - 100)
        self.assertGreater(self.game.snapshot()["height"], 0)
        self.assertFalse(self.game.on_event(follow))
        self.tick(2)
        self.assertEqual(self.game.snapshot()["nitro"], 0)
        self.game.reset()
        self.assertLessEqual(self.game.snapshot()["nitro"], 0)
        self.assertFalse(self.game.on_event(follow))

    def test_follower_nitro_also_lifts_an_airborne_climber(self):
        baseline = Climb(lambda: DEFAULTS)
        for game, followed in ((self.game, True), (baseline, False)):
            game.take_voice()
            while game.snapshot()["standing"] is not None:
                game.advance(1 / 60)
            for _ in range(24):
                game.advance(1 / 60)
            self.assertIsNone(game.snapshot()["standing"])
            if followed:
                self.assertTrue(game.on_event({"type": "follow", "uid": 8, "name": "观众小夏"}))
        for _ in range(30):
            self.game.advance(1 / 60)
            baseline.advance(1 / 60)
        nitro = self.game.snapshot()
        normal = baseline.snapshot()
        self.assertIsNone(normal["standing"])
        self.assertLess(nitro["y"], normal["y"] - 60)

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

    def test_falling_from_a_higher_area_can_reach_the_bottom_platform(self):
        checkpoint = CHECKPOINTS[1]
        start = checkpoint + 8
        self.game.checkpoint = checkpoint
        self.game.record_ledge = self.game.last_ledge = start
        self.game.on_ledge = None
        self.game.flight_target = LEDGES[start + 1]
        self.game.x, self.game.y, self.game.vy = 22, LEDGES[start].y, 100
        self.game.facing = -1
        self.game.camera_y = self.game.y - 548
        self.tick(3)
        mid = self.game.snapshot()
        self.assertLess(mid["y"] - mid["camera"][1], HEIGHT)
        self.assertFalse(mid["recovering"])
        self.assertLess(mid["y"], BASE_Y)
        self.tick(4.2)
        bottom = self.game.snapshot()
        self.assertEqual((bottom["zone"], bottom["standing"], bottom["record"]), ("旧船坞", 0, start))
        self.assertGreater(bottom["fall"], 600)

    def test_water_restarts_from_bottom_even_after_reaching_a_high_area(self):
        start = CHECKPOINTS[2]
        self.game.record_ledge = self.game.last_ledge = self.game.checkpoint = start
        self.game.on_ledge = None
        self.game.flight_target = LEDGES[start + 1]
        self.game.x, self.game.y, self.game.vy = 620, WATER_Y - 5, 250
        self.game.take_voice()
        self.tick(0.1)
        splash = self.game.snapshot()
        self.assertTrue(splash["recovering"])
        self.assertEqual((splash["y"], splash["zone"]), (WATER_Y, "旧船坞"))
        self.assertIn("最底层", self.game.take_voice().fact)
        self.tick(1.5)
        bottom = self.game.snapshot()
        self.assertEqual((bottom["standing"], bottom["record"]), (0, start))

    def test_paid_oil_gift_reduces_launch_lift_once(self):
        self.config["oil_gift"] = "测试抹油"
        gift = {"type": "gift", "gift": "测试抹油", "paid": True, "gift_tid": "oil-1", "name": "观众甲"}
        self.assertFalse(self.game.on_event({**gift, "paid": False}))
        self.assertTrue(self.game.on_event(gift))
        baseline = Climb(lambda: DEFAULTS)
        for _ in range(120):
            self.game.advance(1 / 60)
            baseline.advance(1 / 60)
        state = self.game.snapshot()
        self.assertEqual((state["turns"], state["slips"]), (1, 1))
        self.assertGreater(state["vy"], baseline.snapshot()["vy"] + 100)
        self.assertIn("观众甲", state["notice"])
        self.assertNotIn("oil", state["effects"])
        self.assertFalse(self.game.on_event(gift))

    def test_rocket_gift_launches_a_strong_boost(self):
        self.config["rocket_gift"] = "测试火箭"
        gift = {"type": "gift", "gift": "测试火箭", "paid": True, "gift_tid": "rocket-1", "name": "观众丙"}
        self.assertFalse(self.game.on_event({**gift, "paid": False}))
        self.assertTrue(self.game.on_event(gift))
        state = self.game.snapshot()
        self.assertGreater(state["rocket"], 0)
        self.assertIsNone(state["standing"])
        self.assertEqual(state["turns"], 1)
        self.assertIn("观众丙", state["notice"])
        self.tick(0.4)
        self.assertGreater(self.game.snapshot()["height"], 3)
        self.assertFalse(self.game.on_event(gift))

    def test_magnet_gift_stops_slips_and_spends_one_charge_per_jump(self):
        game = Climb(lambda: {**DEFAULTS, "magnet_gift": "测试磁铁"})
        gift = {"type": "gift", "gift": "测试磁铁", "paid": True, "gift_tid": "magnet-1", "name": "观众丁"}
        self.assertTrue(game.on_event(gift))
        self.assertEqual(game.snapshot()["effects"]["magnet"]["jumps"], 3)
        game.last_ledge = game.record_ledge = game.on_ledge = 2
        ledge = LEDGES[2]
        game.x, game.y = ledge.center, ledge.surface(ledge.center)
        game.landed_at = game.run_clock - 2
        game.auto_at = game.run_clock + 0.7
        for _ in range(300):
            game.advance(1 / 60)
            if game.turns:
                break
        state = game.snapshot()
        self.assertEqual(state["turns"], 1)
        self.assertEqual(state["slips"], 0)
        self.assertEqual(state["effects"]["magnet"]["jumps"], 2)
        self.assertTrue(game.magnet_flight)

    def test_magnet_flight_hooks_a_lower_rim_instead_of_falling(self):
        ledge = LEDGES[1]
        self.game.on_ledge = None
        self.game.magnet_flight = True
        self.game.replan = True
        self.game.x, self.game.y = ledge.right - 80, ledge.y - 30
        self.game.vx, self.game.vy = 0, 100
        self.game.cup_at = (self.game.x, self.game.y - 30)
        self.assertTrue(self.game._fall_catch())
        self.assertIsNotNone(self.game.rim_grab)

    def test_feather_gift_returns_the_faller_to_the_record_ledge(self):
        self.config["feather_gift"] = "测试羽毛"
        gift = {"type": "gift", "gift": "测试羽毛", "paid": True, "gift_tid": "feather-1", "name": "观众戊"}
        self.assertTrue(self.game.on_event(gift))
        self.assertIn("feather", self.game.snapshot()["effects"])
        self.game.record_ledge = self.game.last_ledge = 5
        self.game.checkpoint = 0
        self.game.on_ledge = None
        self.game.flight_target = LEDGES[6]
        self.game.x, self.game.y, self.game.vy = 620, WATER_Y - 5, 250
        self.game.take_voice()
        self.tick(0.1)
        self.assertTrue(self.game.snapshot()["recovering"])
        self.assertNotIn("feather", self.game.snapshot()["effects"])
        self.assertIn("金羽毛", self.game.take_voice().fact)
        self.tick(1.5)
        self.assertEqual(self.game.snapshot()["standing"], 5)

    def test_sabotage_takes_turns_and_wind_pushes_against_launch(self):
        self.config["squall_gift"] = "测试逆风"
        gift = {"type": "gift", "gift": "测试逆风", "paid": True, "gift_tid": "wind-1", "name": "观众乙"}
        self.assertTrue(self.game.on_event(gift))
        self.assertTrue(self.game.simulate("oil", 0))
        self.assertFalse(self.game.simulate("oil", 1))
        self.game.reset()
        self.tick(2.5)
        windy = self.game.snapshot()
        self.assertLess(windy["gust"], 0)
        self.assertLess(windy["vx"], 250)
        self.assertIn("观众乙", windy["notice"])
        self.assertEqual(windy["slips"], 0)
        self.assertIn("oil", windy["effects"])
        self.assertNotIn("squall", windy["effects"])
        self.tick(7.5)
        state = self.game.snapshot()
        self.assertEqual((state["turns"], state["slips"], state["gust"]), (2, 1, 0))
        self.assertNotIn("oil", state["effects"])

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
        brain_patch = patch("climb_voice.Brain")
        self.brain = brain_patch.start().return_value
        self.addCleanup(brain_patch.stop)
        self.brain.ai = AsyncMock(return_value=("这盆真要爬上山，今天我就给它封个冠军。", "开心"))
        self.brain.close = AsyncMock()
        app = self.app = object.__new__(main.App)
        app.cfg = copy.deepcopy(config.DEFAULTS)
        app.cfg["game"]["active"] = "climb"
        app.dt = app.dt_task = None
        app.climb = Climb(lambda: app.cfg["climb"])
        app.climb_scene = ClimbScene(app.climb, config.WEB_DIR)
        app.rice_gift = Mock()
        app.pipe = Mock(paused=False)
        app.pipe.current = None
        app.pipe.pending_replies = set()
        app.pipe.tts_ready = asyncio.Event()
        app.pipe.tts_ready.set()
        app.engine_ready = asyncio.Event()
        app.engine_ready.set()
        app.engine_idle = app._engine_booting = False
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
            await socket.send_json({"action": "weather", "kind": "fog"})
            await asyncio.sleep(0.05)
            self.assertEqual(self.app.climb.snapshot()["weather"]["kind"], "fog")
            self.app.on_event({"type": "follow", "uid": 9, "name": "新观众"})
            self.assertGreater(self.app.climb.snapshot()["nitro"], 0)
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

    async def test_voice_waits_for_engine_without_stalling_the_scene(self):
        self.app.engine_ready.clear()
        self.app.engine_idle = True

        async def warmup():
            self.app.engine_idle = False
            self.app._engine_booting = True

        self.app.start_engine_for_live = AsyncMock(side_effect=warmup)
        self.app.broadcast.capture.previewing = True
        await asyncio.sleep(0.15)
        self.assertGreater(self.app.climb.snapshot()["clock"], 0)
        self.app.start_engine_for_live.assert_awaited_once()
        self.app.pipe.narrate_fresh.assert_not_called()
        state = await (await self.client.get("/api/climb/state")).json()
        self.assertTrue(state["warming_up"])
        self.app._engine_booting = False
        self.app.engine_ready.set()
        await asyncio.sleep(0.1)
        self.app.pipe.narrate_fresh.assert_called_once()
        self.assertIn("给它封个冠军", self.app.pipe.narrate_fresh.call_args.args[0])
        cfg, prompt = self.brain.ai.await_args.args
        self.assertIn("卢本伟", cfg["ai"]["persona"])
        self.assertIn("靠吸盘杆爬上山顶", prompt)

    async def test_repeated_generated_line_does_not_enter_voice_queue(self):
        presenter = ClimbPresenter(self.app)
        self.addAsyncCleanup(presenter.close)
        cue = self.app.climb.take_voice()
        presenter.recent.append("这盆到山顶就封冠军。")
        result = asyncio.get_running_loop().create_future()
        result.set_result(("这盆到山顶就封冠军！", "开心"))
        presenter.jobs["reaction"] = (cue, result)
        presenter._collect(1)
        self.assertEqual(presenter.drafts, {})
        self.app.pipe.narrate_fresh.assert_not_called()

    async def test_duplicate_gift_mappings_are_rejected(self):
        with patch("config.save") as save:
            result = await self.client.post("/api/config", json={"climb": {"boat_gift": "同一种", "oil_gift": "同一种"}},
                                            headers={"X-BVB": "1"})
        self.assertEqual(result.status, 400)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
