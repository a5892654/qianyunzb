"""选角防挂机回归：只用假桥，不启动游戏、不读写玩家存档或直播配置。"""

import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dtgame import view
from dtgame.bridge import BridgeError, GameBridge
from dtgame.player import Player


ROSTER = [
    {"slot": 0, "id": "starter", "name": "奈尔", "unlocked": True},
    {"slot": 1, "id": "yang", "name": "杨", "locked": True},
    {"slot": 2, "id": "alice", "name": "爱丽丝", "unlocked": True},
]


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    def player(self, roster=None, *, used=()):
        bridge = Mock()
        state = {"screen": "character_select", "character_select": {
            "characters": copy.deepcopy(ROSTER if roster is None else roster)}}
        bridge.state.return_value = state
        bridge.screen.side_effect = lambda: {"screen": bridge.state.return_value["screen"]}
        def pick(slot, cid):
            bridge.state.return_value = {"screen": "map"}
            return {"ok": True}
        bridge.pick_character.side_effect = pick
        player = Player({}, bridge, chars=used, speak=Mock(return_value=None), log_push=Mock())
        player._ask = AsyncMock(return_value={"action": "character", "slot": 1, "say": "杨上场"})
        player.running = True
        player.CHARACTER_TRANSITION_TIMEOUT = 0
        return player, bridge, state

    async def test_locked_model_choice_automatically_uses_unlocked_alternative(self):
        player, bridge, _ = self.player(used=["奈尔"])
        result = await player.step()
        self.assertTrue(result["ok"])
        bridge.pick_character.assert_called_once_with(2, "alice")
        self.assertEqual(player.chars, ["奈尔", "爱丽丝"])
        self.assertIn("爱丽丝", result["say"])
        self.assertNotIn("杨", result["say"])
        player.speak.assert_called_once_with(result["say"], "")

    async def test_repeated_model_locked_choice_never_stops_multi_run_rotation(self):
        player, bridge, state = self.player(used=["奈尔"])
        for slot, cid in ((2, "alice"), (0, "starter"), (2, "alice"), (0, "starter")):
            bridge.state.return_value = state
            result = await player.step()
            self.assertTrue(result["ok"])
            bridge.pick_character.assert_called_with(slot, cid)
            self.assertTrue(player.running)
        self.assertEqual((player.fails, player.error_streak), (0, 0))

    async def test_only_one_unlocked_character_can_be_reused(self):
        roster = copy.deepcopy(ROSTER)
        roster[2]["unlocked"] = False
        player, bridge, _ = self.player(roster, used=["奈尔"])
        self.assertTrue((await player.step())["ok"])
        bridge.pick_character.assert_called_once_with(0, "starter")

    async def test_wrong_action_bad_slot_or_missing_id_recovers(self):
        for action in ({"action": "wait"}, {"action": "character", "slot": "bad"},
                       {"action": "character", "slot": 0, "id": "missing"},
                       {"action": "character", "slot": None}):
            with self.subTest(action=action):
                player, bridge, _ = self.player()
                player._ask.return_value = action
                self.assertTrue((await player.step())["ok"])
                bridge.pick_character.assert_called_once_with(0, "starter")

    async def test_model_failure_or_timeout_uses_code_fallback(self):
        for error in (RuntimeError("模型没给 JSON"), asyncio.TimeoutError()):
            with self.subTest(error=type(error)):
                player, bridge, _ = self.player()
                player._ask.side_effect = error
                player.ai_fails = player.AI_FAIL_LIMIT - 1
                self.assertTrue((await player.step())["ok"])
                bridge.pick_character.assert_called_once_with(0, "starter")
                self.assertTrue(player.running)
                self.assertEqual(player.ai_fails, 0)

    async def test_slow_model_is_cancelled_at_selection_deadline(self):
        player, bridge, _ = self.player()
        player.CHARACTER_AI_TIMEOUT = 0.01
        cancelled = asyncio.Event()
        async def slow(text):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        player._ask.side_effect = slow
        result = await asyncio.wait_for(player.step(), timeout=1)
        self.assertTrue(result["ok"])
        self.assertTrue(cancelled.is_set())
        bridge.pick_character.assert_called_once_with(0, "starter")

    async def test_old_bridge_missing_unlock_flags_rejection_skips_model_on_retry(self):
        roster = [{k: v for k, v in c.items() if k not in ("unlocked", "locked")} for c in ROSTER]
        player, bridge, state = self.player(roster)
        enter_map = bridge.pick_character.side_effect
        bridge.pick_character.side_effect = None
        bridge.pick_character.return_value = {"ok": False, "error": "角色尚未解锁"}
        first = await player.step()
        self.assertFalse(first["ok"])
        self.assertTrue(first["recovering"])
        self.assertEqual(player.chars, [])
        player.speak.assert_not_called()
        player._ask.assert_awaited_once()
        self.assertIn("✕不可选（角色尚未解锁）", view.render(player._character_state(state)))
        bridge.pick_character.side_effect = enter_map
        second = await player.step()
        self.assertTrue(second["ok"])
        bridge.pick_character.assert_called_with(0, "starter")
        player._ask.assert_awaited_once()

    async def test_reported_success_without_leaving_selection_is_not_success(self):
        roster = [{k: v for k, v in c.items() if k not in ("unlocked", "locked")} for c in ROSTER]
        player, bridge, _ = self.player(roster)
        enter_map = bridge.pick_character.side_effect
        bridge.pick_character.side_effect = None
        bridge.pick_character.return_value = {"ok": True}
        on_chars = player.on_chars = Mock()
        result = await player.step()
        self.assertFalse(result["ok"])
        self.assertTrue(result["recovering"])
        self.assertIn("仍未进入游戏", result["error"])
        self.assertEqual(player.turns_taken, 0)
        on_chars.assert_not_called()
        player.speak.assert_not_called()
        bridge.pick_character.side_effect = enter_map
        self.assertTrue((await player.step())["ok"])
        bridge.pick_character.assert_called_with(0, "starter")

    async def test_explicit_unlock_status_is_preferred_over_unknown(self):
        roster = [ROSTER[0], {"slot": 1, "id": "yang", "name": "杨"}]
        player, bridge, _ = self.player(roster, used=["奈尔"])
        self.assertTrue((await player.step())["ok"])
        bridge.pick_character.assert_called_once_with(0, "starter")

    async def test_rotation_prefers_known_unlocked_over_unseen_unknown(self):
        roster = [ROSTER[0], {"slot": 1, "id": "yang", "name": "杨"}, ROSTER[2]]
        player, bridge, _ = self.player(roster, used=["爱丽丝", "奈尔"])
        player._ask.return_value = {"action": "character", "slot": 0}
        self.assertTrue((await player.step())["ok"])
        bridge.pick_character.assert_called_once_with(2, "alice")

    async def test_rotation_keeps_only_known_unlocked_character_instead_of_unknown(self):
        player, bridge, state = self.player(
            [ROSTER[0], {"slot": 1, "id": "yang", "name": "杨"}], used=["奈尔"])
        player._ask.return_value = {"action": "character", "slot": 0}
        self.assertIn("只有奈尔可选", player._rotation(state))
        self.assertTrue((await player.step())["ok"])
        bridge.pick_character.assert_called_once_with(0, "starter")

    async def test_empty_or_fully_locked_roster_stops_with_visible_error_without_clicking(self):
        for roster in ([], [{**c, "locked": True} for c in ROSTER]):
            player, bridge, _ = self.player(roster)
            result = await player.step()
            self.assertFalse(result["ok"])
            self.assertFalse(result["recovering"])
            self.assertFalse(player.running)
            bridge.pick_character.assert_not_called()
            player._ask.assert_not_awaited()
            self.assertEqual(player.log_push.call_args.args[0], "error")

    async def test_exhausts_unknown_roster_once_without_infinite_retry(self):
        roster = [{"slot": c["slot"], "id": c["id"], "name": c["name"]} for c in ROSTER]
        player, bridge, _ = self.player(roster)
        bridge.pick_character.side_effect = None
        bridge.pick_character.return_value = {"ok": False, "error": "角色锁定"}
        for _ in roster:
            self.assertFalse((await player.step())["ok"])
        self.assertFalse(player.running)
        attempted = [call.args[1] for call in bridge.pick_character.call_args_list]
        self.assertEqual(len(set(attempted)), len(roster))
        player._ask.assert_awaited_once()

    async def test_stale_response_does_not_blacklist_character(self):
        player, bridge, _ = self.player()
        bridge.pick_character.side_effect = None
        bridge.pick_character.return_value = {"ok": False, "stale": True}
        self.assertTrue((await player.step())["stale"])
        self.assertEqual(player._character_rejected, {})

    async def test_transport_failure_does_not_blacklist_character(self):
        player, bridge, _ = self.player()
        bridge.pick_character.side_effect = BridgeError("连接超时")
        result = await player.step()
        self.assertTrue(result["recovering"])
        self.assertEqual(player._character_rejected, {})
        player.speak.assert_not_called()

    async def test_busy_rejection_does_not_blacklist_character(self):
        player, bridge, _ = self.player()
        bridge.pick_character.side_effect = None
        bridge.pick_character.return_value = {"ok": False, "busy": True, "error": "转场中"}
        self.assertTrue((await player.step())["recovering"])
        self.assertEqual(player._character_rejected, {})

    async def test_becoming_busy_during_model_decision_does_not_send_click(self):
        player, bridge, _ = self.player()
        bridge.screen.side_effect = None
        bridge.screen.return_value = {"screen": "character_select", "busy": True}
        self.assertTrue((await player.step())["stale"])
        bridge.pick_character.assert_not_called()

    async def test_new_screen_invalidates_model_choice(self):
        player, bridge, _ = self.player()
        bridge.screen.side_effect = None
        bridge.screen.return_value = {"screen": "map"}
        self.assertTrue((await player.step())["stale"])
        bridge.pick_character.assert_not_called()

    async def test_transition_is_allowed_to_finish_before_rejecting(self):
        player, bridge, _ = self.player()
        player.CHARACTER_TRANSITION_TIMEOUT = 1
        bridge.pick_character.side_effect = None
        bridge.pick_character.return_value = {"ok": True}
        bridge.screen.side_effect = [
            {"screen": "character_select"}, {"screen": "character_select", "busy": True},
            {"screen": "map"}]
        result = await player.step()
        self.assertTrue(result["ok"])
        self.assertEqual(bridge.pick_character.call_count, 1)
        self.assertEqual(player._character_rejected, {})

    async def test_busy_transition_does_not_reject_or_narrate_false_success(self):
        player, bridge, _ = self.player()
        bridge.pick_character.side_effect = None
        bridge.pick_character.return_value = {"ok": True}
        bridge.screen.side_effect = [
            {"screen": "character_select"}, {"screen": "character_select", "busy": True}]
        result = await player.step()
        self.assertTrue(result["stale"])
        self.assertEqual(player._character_rejected, {})
        player.speak.assert_not_called()

    async def test_gameplay_clears_rejections_for_next_selection(self):
        player, bridge, state = self.player()
        player._reject_character(ROSTER[0], "上一轮被拒绝")
        bridge.state.return_value = {"screen": "map"}
        bridge.choose_node.return_value = {"ok": True}
        player._ask.return_value = {"action": "node", "col": 0, "row": 0}
        self.assertTrue((await player.step())["ok"])
        self.assertEqual(player._character_rejected, {})


class ViewTests(unittest.TestCase):
    def test_all_lock_flags_render_as_unavailable(self):
        for flags in ({"locked": True}, {"unlocked": False}, {"enabled": False}):
            character = {"slot": 0, "name": "杨", **flags}
            self.assertFalse(view.character_selectable(character))
            self.assertIn("✕不可选", view.render({"screen": "character_select", "character_select": {
                "characters": [character]}}))

    def test_unknown_unlock_status_is_explicit_in_model_text(self):
        text = view.render({"screen": "character_select", "character_select": {
            "characters": [{"slot": 0, "name": "杨"}]}})
        self.assertIn("解锁状态未知", text)


class BridgeFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_wire_protocol_recovers_from_locked_character_silent_noop(self):
        """复现 CharacterSelect._confirm_verified 对锁定角色直接返回的旧桥行为。"""
        screen = "character_select"
        commands = []
        roster = [{"slot": c["slot"], "id": c["id"], "name": c["name"]} for c in ROSTER]

        async def handle(reader, writer):
            nonlocal screen
            request = json.loads(await reader.readline())
            cmd, args = request["cmd"], request["args"]
            commands.append((cmd, args))
            if cmd == "state":
                response = {"state": {"screen": screen, "character_select": {"characters": roster}}}
            elif cmd == "screen":
                response = {"screen": {"screen": screen}}
            elif cmd == "pick_character":
                # 锁定的杨：桥返回 ok，但游戏不离开选角页；默认角色才能正常出征。
                if args["id"] == "starter":
                    screen = "map"
                response = {"ok": True}
            else:
                response = {"ok": False, "error": "测试桥不支持这个动作"}
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            bridge = GameBridge(port=server.sockets[0].getsockname()[1])
            player = Player({}, bridge, narrate=False)
            player.CHARACTER_TRANSITION_TIMEOUT = 0
            player._ask = AsyncMock(return_value={"action": "character", "slot": 1})
            first = await player.step()
            self.assertFalse(first["ok"])
            self.assertTrue(first["recovering"])
            self.assertEqual(player.chars, [])
            second = await player.step()
            self.assertTrue(second["ok"])
            self.assertEqual(second["screen_after"], "map")
            self.assertEqual(player.chars, ["奈尔"])
            player._ask.assert_awaited_once()
        self.assertEqual([args for cmd, args in commands if cmd == "pick_character"], [
            {"slot": 1, "id": "yang"}, {"slot": 0, "id": "starter"}])
        self.assertEqual({cmd for cmd, _ in commands}, {"state", "screen", "pick_character"})


if __name__ == "__main__":
    unittest.main()
