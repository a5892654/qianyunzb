"""AI 深度选择回归；使用假桥/本机协议模拟，不启动游戏或访问玩家存档。"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dtgame import view
from dtgame.bridge import BridgeError, GameBridge
from dtgame.expedition import choose_depth, depth_settings
from dtgame.mcp_server import Server, TOOLS
from dtgame.player import Player, SYSTEM


def hall_state(**over):
    return {"screen": "hall", "hall": {"selected_depth": 2, "unlocked_depth": 5,
                                        "expedition_mode": "deep", **over}}


class DepthTests(unittest.TestCase):
    def test_caps_at_actual_unlock_limit_and_game_max(self):
        self.assertEqual(choose_depth(hall_state(), 19), 5)
        self.assertEqual(choose_depth(hall_state(), -1), 0)
        self.assertEqual(choose_depth(hall_state(unlocked_depth=100), 99), 20)
        self.assertEqual(choose_depth(hall_state(unlocked_depth=0), 4), 0)

    def test_malformed_depth_uses_current_valid_value(self):
        for requested in (None, True, "4", 2.9, [], {}):
            with self.subTest(requested=requested):
                self.assertEqual(choose_depth(hall_state(), requested), 2)
        self.assertEqual(choose_depth(hall_state(selected_depth=None), None, 3), 3)

    def test_unknown_unlock_range_never_guesses(self):
        for limit in (None, "20", True):
            self.assertIsNone(choose_depth(hall_state(unlocked_depth=limit), 20))
        self.assertIsNone(choose_depth({"screen": "hall"}, 20))

    def test_shallow_route_is_fixed_to_zero(self):
        state = hall_state(expedition_mode="shallow")
        self.assertEqual(depth_settings(state), (0, 0))
        self.assertEqual(choose_depth(state, 5), 0)

    def test_legacy_selected_depth_name(self):
        self.assertEqual(depth_settings({"hall": {"depth": 3, "unlocked_depth": 6}}), (3, 6))

    def test_model_sees_depth_range_rules_and_current_run_depth(self):
        text = view.render(hall_state(depth_debuffs=[
            {"depth": 1, "name": "精英猎场", "desc": "精英出现率增加"}]))
        self.assertIn("当前选择深度: 2", text)
        self.assertIn("0～5", text)
        self.assertIn("精英猎场", text)
        self.assertIn("action=expedition", text)
        self.assertIn("未提供已解锁深度上限", view.render({"screen": "hall"}))
        self.assertIn("深度 0", view.render(hall_state(expedition_mode="shallow")))
        self.assertIn("出征深度 3", view.render({"screen": "other", "run": {"depth": 3}}))
        self.assertIn('"action":"expedition"', SYSTEM)
        self.assertIn("不能中途改本局难度", SYSTEM)


class PlayerDepthTests(unittest.IsolatedAsyncioTestCase):
    def player(self, state=None, **kwargs):
        bridge = Mock()
        bridge.state.return_value = hall_state() if state is None else state
        bridge.screen.return_value = {"screen": "hall"}
        bridge.begin_expedition.return_value = {"ok": True, "screen_after": "character_select"}
        def depart(*args):
            bridge.screen.return_value = {"screen": "character_select"}
            return {"ok": True}
        bridge.begin_expedition.side_effect = depart
        player = Player({}, bridge, speak=Mock(return_value=None), log_push=Mock(), **kwargs)
        player.EXPEDITION_TRANSITION_TIMEOUT = 0
        player._ask = AsyncMock(return_value={"action": "expedition", "depth": 4, "say": "这局往下再探探"})
        player.running = True
        return player, bridge

    async def test_ai_depth_is_passed_to_game(self):
        player, bridge = self.player()
        result = await player.step()
        self.assertTrue(result["ok"])
        bridge.begin_expedition.assert_called_once_with(4)
        self.assertIn("0～5", player._ask.call_args.args[0])
        player.speak.assert_called_once_with("这局往下再探探", "")

    async def test_locked_depth_is_clamped_and_narration_corrected(self):
        player, bridge = self.player()
        player._ask.return_value = {"action": "expedition", "depth": 20, "say": "这局深度二十"}
        result = await player.step()
        bridge.begin_expedition.assert_called_once_with(5)
        self.assertEqual(result["action"]["depth"], 5)
        self.assertIn("深度5", result["say"])
        self.assertNotIn("二十", result["say"])

    async def test_invalid_action_still_departs_with_current_depth(self):
        player, bridge = self.player()
        player._ask.return_value = {"action": "wait"}
        self.assertTrue((await player.step())["ok"])
        bridge.begin_expedition.assert_called_once_with(2)

    async def test_model_failure_uses_current_depth_without_stopping(self):
        for error in (RuntimeError("坏 JSON"), asyncio.TimeoutError()):
            player, bridge = self.player()
            player._ask.side_effect = error
            player.ai_fails = player.AI_FAIL_LIMIT - 1
            self.assertTrue((await player.step())["ok"])
            bridge.begin_expedition.assert_called_once_with(2)
            self.assertTrue(player.running)
            self.assertEqual(player.ai_fails, 0)

    async def test_slow_model_is_bounded(self):
        player, bridge = self.player()
        player.EXPEDITION_AI_TIMEOUT = 0.01
        cancelled = asyncio.Event()
        async def slow(text):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        player._ask.side_effect = slow
        self.assertTrue((await asyncio.wait_for(player.step(), 1))["ok"])
        self.assertTrue(cancelled.is_set())
        bridge.begin_expedition.assert_called_once_with(2)

    async def test_old_bridge_preserves_current_setting_without_guessing_or_model(self):
        player, bridge = self.player({"screen": "hall", "hall": {"depth": 7}})
        result = await player.step()
        self.assertTrue(result["ok"])
        bridge.begin_expedition.assert_called_once_with()
        player._ask.assert_not_awaited()
        self.assertNotIn("depth", result["action"])

    async def test_fixed_configuration_is_clamped_to_unlocked_range(self):
        player, bridge = self.player(auto_depth=False, depth=20)
        self.assertTrue((await player.step())["ok"])
        bridge.begin_expedition.assert_called_once_with(5)
        player._ask.assert_not_awaited()

    async def test_shallow_mode_never_sends_nonzero_depth(self):
        player, bridge = self.player(hall_state(expedition_mode="shallow"))
        self.assertTrue((await player.step())["ok"])
        bridge.begin_expedition.assert_called_once_with(0)

    async def test_rejected_depth_retries_zero_without_another_model_call(self):
        player, bridge = self.player()
        def depart(depth):
            if depth != 0:
                return {"ok": False, "error": "深度不可用"}
            bridge.screen.return_value = {"screen": "character_select"}
            return {"ok": True}
        bridge.begin_expedition.side_effect = depart
        self.assertFalse((await player.step())["ok"])
        player.speak.assert_not_called()
        self.assertTrue((await player.step())["ok"])
        self.assertEqual([call.args for call in bridge.begin_expedition.call_args_list], [(4,), (0,)])
        player._ask.assert_awaited_once()

    async def test_silent_noop_does_not_report_or_record_success(self):
        player, bridge = self.player()
        bridge.begin_expedition.side_effect = None
        self.assertFalse((await player.step())["ok"])
        self.assertIsNone(player._expedition_depth)
        player.speak.assert_not_called()
        self.assertTrue(player._expedition_recovery)

    async def test_new_dialog_is_handled_without_false_departure_narration(self):
        player, bridge = self.player()
        def show_dialog(depth):
            bridge.screen.return_value = {"screen": "dialog"}
            return {"ok": True}
        bridge.begin_expedition.side_effect = show_dialog
        self.assertTrue((await player.step())["stale"])
        self.assertIsNone(player._expedition_depth)
        player.speak.assert_not_called()

    async def test_fixed_depth_is_kept_after_rejection(self):
        player, bridge = self.player(auto_depth=False, depth=3)
        bridge.begin_expedition.side_effect = None
        bridge.begin_expedition.return_value = {"ok": False, "error": "账号需验证"}
        await player.step()
        await player.step()
        self.assertEqual([call.args for call in bridge.begin_expedition.call_args_list], [(3,), (3,)])

    async def test_stale_or_busy_state_does_not_start_an_expedition(self):
        for screen in ({"screen": "map"}, {"screen": "hall", "busy": True}):
            player, bridge = self.player()
            bridge.screen.return_value = screen
            self.assertTrue((await player.step())["stale"])
            bridge.begin_expedition.assert_not_called()

    async def test_existing_run_never_gets_restarted_to_change_depth(self):
        player, bridge = self.player()
        result = player._do({"screen": "battle"}, {"action": "expedition", "depth": 5})
        self.assertFalse(result["ok"])
        bridge.begin_expedition.assert_not_called()

    async def test_previous_defeat_is_visible_in_next_hall_decision(self):
        player, bridge = self.player()
        player._expedition_depth = 4
        bridge.state.return_value = {"screen": "death", "run": {"revives": 2, "character": "奈尔"}}
        bridge.return_to_hall.return_value = {"ok": True}
        await player.step()
        bridge.state.return_value = hall_state()
        await player.step()
        text = player._ask.call_args.args[0]
        self.assertIn("上一局表现", text)
        self.assertIn('"depth": 4', text)
        self.assertIn("阵亡", text)

    async def test_hall_is_handled_by_model_step_in_run_loop(self):
        player, bridge = self.player()
        player._boot = Mock()
        def depart(depth):
            player.stop()
            bridge.screen.return_value = {"screen": "character_select"}
            return {"ok": True}
        bridge.begin_expedition.side_effect = depart
        await asyncio.wait_for(player.run_forever(interval=0), 1)
        player._boot.assert_not_called()
        bridge.begin_expedition.assert_called_once_with(4)


class McpDepthTests(unittest.TestCase):
    def server(self, state=None):
        server = Server()
        server.bridge = Mock()
        server.bridge.state.return_value = hall_state() if state is None else state
        server.bridge.begin_expedition.return_value = {"ok": True}
        return server

    def test_tool_is_advertised_and_valid_depth_is_forwarded(self):
        spec = next(t for t in TOOLS if t["name"] == "begin_expedition")
        self.assertEqual(spec["inputSchema"]["properties"]["depth"]["maximum"], 20)
        server = self.server()
        self.assertTrue(json.loads(server.call_tool("begin_expedition", {"depth": 3}))["ok"])
        server.bridge.begin_expedition.assert_called_once_with(3)

    def test_mcp_rejects_locked_or_malformed_depths(self):
        for depth in (6, -1, True, "3", 2.5):
            server = self.server()
            with self.assertRaises(ValueError):
                server.call_tool("begin_expedition", {"depth": depth})
            server.bridge.begin_expedition.assert_not_called()

    def test_mcp_missing_unlock_limit_or_wrong_screen_does_not_mutate(self):
        for state in ({"screen": "hall"}, {"screen": "battle"}, {**hall_state(), "busy": True}):
            server = self.server(state)
            with self.assertRaises(BridgeError):
                server.call_tool("begin_expedition", {"depth": 3})
            server.bridge.begin_expedition.assert_not_called()

    def test_mcp_can_omit_depth_for_legacy_bridge(self):
        server = self.server({"screen": "hall"})
        server.call_tool("begin_expedition", {})
        server.bridge.begin_expedition.assert_called_once_with()


class WireDepthTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_bridge_protocol_carries_clamped_ai_depth(self):
        mutations = []
        screen = "hall"
        async def handle(reader, writer):
            nonlocal screen
            request = json.loads(await reader.readline())
            if request["cmd"] == "state":
                response = {"state": hall_state(unlocked_depth=3)}
            elif request["cmd"] == "screen":
                response = {"screen": {"screen": screen}}
            elif request["cmd"] == "begin_expedition":
                mutations.append(request["args"])
                screen = "character_select"
                response = {"ok": True, "screen_after": "character_select"}
            else:
                response = {"ok": False}
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            player = Player({}, GameBridge(port=server.sockets[0].getsockname()[1]), narrate=False)
            player._ask = AsyncMock(return_value={"action": "expedition", "depth": 10})
            self.assertTrue((await player.step())["ok"])
        self.assertEqual(mutations, [{"depth": 3}])


if __name__ == "__main__":
    unittest.main()
