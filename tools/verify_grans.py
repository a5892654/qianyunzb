"""格兰斯代打协议回归，不连接实际游戏或读写玩家存档。"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dtgame import grans, view
from dtgame.bridge import BridgeError, GameBridge
from dtgame.mcp_server import Server, TOOLS
from dtgame.player import Player
from tools.install_grans_bridge import install, ENTRY


def snapshot(release=False, side="light", can_release=True):
    return {"ok": True, "protocol": grans.PROTOCOL, "character_id": "twilight_grans",
            "battle_id": "battle1", "turn": 2, "hand_ids": ["grans_001__event_1"],
            "light": 2, "dark": 3, "capacity": 6, "release_mode": release,
            "can_switch": True, "busy": False, "cards": [{
                "slot": 0, "id": "grans_001__event_1", "name": "光剑", "sword": side,
                "can_release": can_release, "cross_charge_available": True, "pair_spend": 2,
                "normal_desc": "造成7点伤害", "release_desc": "消耗2光蓄，另获6固定格挡",
                "preview": {"n": 2, "damage": 15, "fixed_block": 6, "draw": 0,
                            "light_after": 0, "dark_after": 4}}]}


def state():
    return {"screen": "battle", "run": {"character": "光暗双剑·格兰斯"}, "battle": {
        "turn": 2, "energy": 3, "hp": 30, "hand": [
            {"slot": 0, "id": "grans_001__event_1", "name": "光剑", "cost": 1, "desc": "旧文案"}]}}


class SnapshotTests(unittest.TestCase):
    def test_only_actual_grans_gets_character_prompt(self):
        self.assertTrue(grans.is_grans(state()))
        other = state()
        other["run"]["character"] = "龙枪骑士·奈尔"
        self.assertFalse(grans.is_grans(other))
        player = Player({}, Mock())
        normal = player._system_prompt()
        player._last_state = state()
        prompt = player._system_prompt()
        self.assertNotIn("全手牌共用开关", normal)
        self.assertIn('"release":false', prompt)
        self.assertIn('"release":true', prompt)
        self.assertIn("跨回合保留", prompt)
        self.assertIn("合璧牌始终", prompt)
        player.emotions = lambda: [{"name": "开心"}]
        self.assertIn("全手牌共用开关", player._system_prompt())
        self.assertIn('"emotion"', player._system_prompt())

    def test_merge_is_read_only_and_uses_exact_instance_id(self):
        original = state()
        before = copy.deepcopy(original)
        merged = grans.merge_snapshot(original, snapshot())
        self.assertEqual(original, before)
        self.assertEqual(merged["battle"]["hand"][0]["grans"]["preview"]["fixed_block"], 6)
        wrong = snapshot()
        wrong["hand_ids"] = ["grans_001__event_other"]
        with self.assertRaises(ValueError):
            grans.merge_snapshot(original, wrong)

    def test_legacy_hand_without_id_requires_same_slot_and_name(self):
        original = state()
        original["battle"]["hand"][0].pop("id")
        self.assertTrue(grans.merge_snapshot(original, snapshot())["battle"]["grans"]["available"])
        original["battle"]["hand"][0]["name"] = "另一张牌"
        with self.assertRaises(ValueError):
            grans.merge_snapshot(original, snapshot())

    def test_busy_snapshot_is_not_clickable(self):
        snap = snapshot()
        snap.update(busy=True, can_switch=False)
        self.assertTrue(grans.merge_snapshot(state(), snap)["busy"])

    def test_different_turn_is_never_merged_even_with_same_retained_hand(self):
        snap = snapshot()
        snap["turn"] = 3
        with self.assertRaises(ValueError):
            grans.merge_snapshot(state(), snap)

    def test_view_explains_banks_modes_and_per_card_previews(self):
        text = view.render(grans.merge_snapshot(state(), snapshot()))
        for expected in ("光蓄 2/6", "暗蓄 3/6", "当前全手牌模式：蓄能", "release=true", "额外6固定格挡", "以出牌后的新状态为准"):
            self.assertIn(expected, text)
        self.assertNotIn("旧文案", text)
        text = view.render(grans.merge_snapshot(state(), snapshot(True, "dark")))
        self.assertIn("当前全手牌模式：解放", text)
        self.assertIn("首段基础伤害15", text)
        self.assertIn("不要重复加算", text)

    def test_dual_and_exhausted_cards_have_explicit_restrictions(self):
        self.assertIn("自动消耗2对", view.render(grans.merge_snapshot(state(), snapshot(side="dual"))))
        self.assertIn("解放不可用", view.render(grans.merge_snapshot(state(), snapshot(can_release=False))))


class BridgeTests(unittest.TestCase):
    def bridge(self, snap=None):
        bridge = GameBridge()
        bridge.call = Mock(return_value={"ok": True})
        bridge._grans_snapshot = snapshot() if snap is None else snap
        def control(cmd, args=None):
            result = copy.deepcopy(bridge._grans_snapshot)
            if cmd == "grans_mode": result["release_mode"] = args["release"]
            return result
        bridge._call_grans = Mock(side_effect=control)
        return bridge

    def test_both_boolean_modes_switch_before_play_and_normalize_id(self):
        for release in (False, True):
            bridge = self.bridge()
            events = []
            original = bridge._call_grans.side_effect
            bridge._call_grans.side_effect = lambda cmd, args: (events.append("switch"), original(cmd, args))[1]
            bridge.call.side_effect = lambda *args: (events.append("play"), {"ok": True})[1]
            self.assertTrue(bridge.play_card(99, "grans_001__event_1", 0, release=release)["ok"])
            self.assertEqual(events, ["switch", "play"])
            bridge.call.assert_called_once_with("play_card", {"slot": 0, "id": "grans_001__event_1", "target": 0})
            self.assertEqual(bridge._call_grans.call_args.args[1]["release"], release)
            self.assertIsNone(bridge._grans_snapshot)

    def test_stale_or_busy_switch_never_sends_play(self):
        for result in ({"ok": False, "stale": True}, {"ok": False, "busy": True}):
            bridge = self.bridge()
            bridge._call_grans.side_effect = None
            bridge._call_grans.return_value = result
            self.assertFalse(bridge.play_card(0, release=True)["ok"])
            bridge.call.assert_not_called()

    def test_empty_bank_copy_and_dual_do_not_spend_or_switch(self):
        for snap in (snapshot(can_release=False), snapshot(side="dual"), snapshot(side="")):
            bridge = self.bridge(snap)
            self.assertFalse(bridge.play_card(0, release=True)["ok"])
            bridge.call.assert_not_called()
            bridge._call_grans.assert_not_called()

    def test_ordinary_play_is_unchanged_and_does_not_use_extension(self):
        bridge = self.bridge()
        bridge.play_card(0, "normal_card", 1)
        bridge._call_grans.assert_not_called()
        bridge.call.assert_called_once_with("play_card", {"slot": 0, "id": "normal_card", "target": 1})

    def test_non_boolean_release_is_rejected_before_any_mutation(self):
        for release in ("false", 1, [], {}):
            bridge = self.bridge()
            with self.assertRaises(ValueError): bridge.play_card(0, release=release)
            bridge.call.assert_not_called()
            bridge._call_grans.assert_not_called()

    def test_missing_extension_is_visible_and_retry_deadline_does_not_slide(self):
        bridge = self.bridge()
        bridge.call.return_value = {"state": state()}
        bridge._call_grans.side_effect = BridgeError("not installed")
        with patch("dtgame.bridge.time.monotonic", return_value=100):
            result = bridge.state()
        self.assertIn("接口不可用", view.render(result))
        with patch("dtgame.bridge.time.monotonic", return_value=105): bridge.state()
        self.assertEqual(bridge._grans_probe_after, 110)
        with patch("dtgame.bridge.time.monotonic", return_value=111): bridge.state()
        self.assertEqual(bridge._call_grans.call_count, 2)

    def test_card_race_is_retried_without_network_backoff(self):
        bridge = self.bridge()
        bridge.call.return_value = {"state": state()}
        bad = snapshot()
        bad["hand_ids"] = ["other"]
        bridge._call_grans.side_effect = [bad, snapshot()]
        self.assertFalse(bridge.state()["battle"]["grans"]["available"])
        self.assertEqual(bridge._grans_probe_after, 0)
        self.assertTrue(bridge.state()["battle"]["grans"]["available"])

    def test_other_character_does_not_probe_extension(self):
        bridge = self.bridge()
        other = state()
        other["run"]["character"] = "奈尔"
        bridge.call.return_value = {"state": other}
        bridge.state()
        bridge._call_grans.assert_not_called()


class PlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_per_card_release_is_forwarded_with_narration_after_success(self):
        for release in (False, True):
            bridge = Mock()
            bridge.state.return_value = grans.merge_snapshot(state(), snapshot())
            bridge.screen.return_value = {"screen": "battle"}
            bridge.play_card.return_value = {"ok": True}
            speak = Mock(return_value=None)
            player = Player({}, bridge, speak=speak)
            player._ask = AsyncMock(return_value={"action": "play", "slot": 0, "target": 0, "release": release, "say": "这一剑接上"})
            self.assertTrue((await player.step())["ok"])
            bridge.play_card.assert_called_once_with(0, "", 0, release=release)
            speak.assert_called_once_with("这一剑接上", "")

    def test_non_grans_or_missing_interface_cannot_request_release(self):
        for current in (state(), {"screen": "battle", "run": {"character": "奈尔"}}):
            bridge = Mock()
            player = Player({}, bridge)
            self.assertFalse(player._do(current, {"action": "play", "slot": 0, "release": True})["ok"])
            bridge.play_card.assert_not_called()


class McpTests(unittest.TestCase):
    def test_schema_and_forwarding_preserve_false(self):
        spec = next(t for t in TOOLS if t["name"] == "play_card")
        self.assertEqual(spec["inputSchema"]["properties"]["release"]["type"], "boolean")
        server = Server()
        server.bridge = Mock()
        server.bridge.play_card.return_value = {"ok": True}
        server.bridge.set_grans_mode.return_value = {"ok": True}
        self.assertTrue(json.loads(server.call_tool("play_card", {"slot": 0, "release": False}))["ok"])
        server.bridge.play_card.assert_called_once_with(0, "", -1, release=False)
        server.call_tool("set_grans_mode", {"release": False})
        server.bridge.state.assert_called_once()
        server.bridge.set_grans_mode.assert_called_once_with(False)


class InstallerTests(unittest.TestCase):
    def test_install_keeps_storage_guard_first_and_is_idempotent_with_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "game"
            (project / "scripts/battle").mkdir(parents=True)
            (project / "scripts/battle/grans_card_effects.gd").write_text("stub")
            settings = project / "project.godot"
            original = '[autoload]\nTestStorageGuard="*res://guard.gd"\n\n[display]\n'
            settings.write_text(original)
            result = install(project)
            text = settings.read_text()
            self.assertLess(text.index("TestStorageGuard="), text.index(ENTRY))
            self.assertLess(text.index(ENTRY), text.index("[display]"))
            self.assertEqual((Path(result["backup"]) / "project.godot").read_text(), original)
            self.assertEqual(install(project)["changed"], [])


if __name__ == "__main__": unittest.main()
