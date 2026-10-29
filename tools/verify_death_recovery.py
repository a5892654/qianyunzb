"""0血未结算防挂机回归；不连接真实账号，不写玩家存档。"""
import asyncio
import copy
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dtgame import lifecycle
from dtgame.bridge import BridgeError, GameBridge
from dtgame.grans import PROTOCOL
from dtgame.player import Player
from tools.verify_grans import snapshot as grans_snapshot, state as grans_state


def life(hp=0, over=False, busy=False):
    return {"ok": True, "protocol": PROTOCOL, "battle_id": "battle1", "turn": 3,
            "hp": hp, "battle_over": over, "busy": busy}


def state(hp=0, *, busy=False, over=False, extension=False):
    battle = {"turn": 3, "hp": hp, "dead": hp is not None and hp <= 0, "battle_over": over,
              "enemies": [{"id": "faor_god_of_completion", "hp": 300}], "hand": []}
    if extension:
        battle["life"] = {**life(hp, over, busy), "available": True}
    return {"screen": "battle", "busy": busy, "battle": battle, "run": {"hp": hp, "character": "奈尔"}}


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def player(self, current=None):
        bridge = Mock()
        bridge.state.return_value = state() if current is None else current
        bridge.screen.return_value = {"screen": "battle"}
        bridge.end_turn.return_value = {"ok": True}
        bridge.confirm_result.return_value = {"ok": True}
        bridge.recover_death.return_value = {"ok": True}
        p = Player({}, bridge, narrate=False, log_push=Mock())
        p._ask = AsyncMock(return_value={"action": "end", "say": ""})
        p.DEATH_SETTLE_GRACE = 0
        p.DEATH_RETRY_SECONDS = 0
        p.running = True
        return p, bridge

    async def test_zero_hp_without_dead_flag_still_advances_turn(self):
        current = state()
        current["battle"].pop("dead")
        p, bridge = self.player(current)
        result = await p.step()
        self.assertEqual(result["action"]["action"], "end")
        bridge.end_turn.assert_called_once()
        bridge.confirm_result.assert_not_called()
        p._ask.assert_not_awaited()

    async def test_dead_flag_and_run_hp_fallback_are_supported(self):
        for current in ({"screen": "battle", "battle": {"dead": True}},
                        {"screen": "battle", "run": {"hp": 0}}, state(-3)):
            p, bridge = self.player(current)
            await p.step()
            bridge.end_turn.assert_called_once()
            p._ask.assert_not_awaited()

    async def test_installed_interface_uses_game_death_check_instead_of_ending_turn(self):
        p, bridge = self.player(state(extension=True))
        result = await p.step()
        self.assertEqual(result["action"]["action"], "recover_death")
        bridge.recover_death.assert_called_once()
        bridge.end_turn.assert_not_called()
        p._ask.assert_not_awaited()

    async def test_battle_already_over_only_confirms_result(self):
        p, bridge = self.player(state(over=True))
        await p.step()
        bridge.confirm_result.assert_called_once()
        bridge.end_turn.assert_not_called()
        bridge.recover_death.assert_not_called()

    async def test_normal_death_gets_a_grace_period(self):
        p, bridge = self.player()
        p.DEATH_SETTLE_GRACE = 1.5
        self.assertEqual((await p.step())["action"]["action"], "wait_death")
        bridge.end_turn.assert_not_called()
        p._death_next_retry = 0
        await p.step()
        bridge.end_turn.assert_called_once()

    async def test_real_animation_waits_but_stuck_busy_flag_cannot_hide_zero_hp_forever(self):
        p, bridge = self.player(state(busy=True))
        self.assertEqual((await p.step())["action"]["action"], "wait_death")
        bridge.end_turn.assert_not_called()
        p._death_since = time.monotonic() - p.DEATH_BUSY_TIMEOUT - 1
        await p.step()
        bridge.end_turn.assert_called_once()

    async def test_retry_is_throttled(self):
        p, bridge = self.player()
        p.DEATH_RETRY_SECONDS = 5
        await p.step()
        self.assertEqual((await p.step())["action"]["action"], "wait_death")
        bridge.end_turn.assert_called_once()

    async def test_still_zero_after_reported_success_is_bounded_and_visible(self):
        p, bridge = self.player()
        for _ in range(p.DEATH_ATTEMPT_LIMIT): await p.step()
        result = await p.step()
        self.assertFalse(p.running)
        self.assertIn("多次推进后仍未完成", result["error"])
        self.assertEqual(bridge.end_turn.call_count + bridge.confirm_result.call_count, p.DEATH_ATTEMPT_LIMIT)
        self.assertEqual(p.log_push.call_args.args[0], "error")
        p._ask.assert_not_awaited()

    async def test_failed_turn_attempt_has_a_result_confirmation_fallback(self):
        p, bridge = self.player()
        bridge.end_turn.return_value = {"ok": False, "battle_over": True}
        await p.step()
        result = await p.step()
        self.assertEqual(result["action"]["action"], "confirm_result")
        bridge.confirm_result.assert_called_once()

    async def test_game_check_without_progress_falls_back_to_public_end_turn(self):
        p, bridge = self.player(state(extension=True))
        await p.step()
        result = await p.step()
        self.assertEqual(result["action"]["action"], "end")
        bridge.recover_death.assert_called_once()
        bridge.end_turn.assert_called_once()

    async def test_revived_before_action_cancels_old_recovery(self):
        p, bridge = self.player()
        bridge.state.side_effect = [state(), state(hp=1)]
        self.assertTrue((await p.step())["stale"])
        bridge.end_turn.assert_not_called()
        bridge.recover_death.assert_not_called()
        p._ask.assert_not_awaited()

    async def test_new_death_panel_or_new_battle_invalidates_recovery(self):
        for latest in ({"screen": "death_analysis"}, {"screen": "death"}, state(extension=True)):
            p, bridge = self.player(state(extension=True))
            if latest.get("battle"):
                latest["battle"]["life"]["battle_id"] = "new_battle"
            bridge.state.side_effect = [state(extension=True), latest]
            self.assertTrue((await p.step())["stale"])
            bridge.end_turn.assert_not_called()
            bridge.recover_death.assert_not_called()

    async def test_healthy_hp_overrides_stale_dead_flag_and_resets_watchdog(self):
        p, bridge = self.player()
        await p.step()
        current = state(hp=1)
        current["battle"]["dead"] = True
        bridge.state.return_value = current
        await p.step()
        p._ask.assert_awaited_once()
        self.assertEqual(p._death_attempts, 0)

    async def test_death_analysis_and_existing_revive_budget_continue_normally(self):
        p, bridge = self.player(state(extension=True))
        def recover():
            bridge.state.return_value = {"screen": "death_analysis"}
            return {"ok": True}
        bridge.recover_death.side_effect = recover
        await p.step()
        bridge.confirm_death_analysis.side_effect = lambda: (
            setattr(bridge.state, "return_value", {"screen": "death", "run": {"revives": 2}}), {"ok": True})[1]
        self.assertEqual((await p.step())["action"]["action"], "confirm_death_analysis")
        self.assertEqual((await p.step())["action"]["action"], "return_to_hall")
        bridge.revive.assert_not_called()

    async def test_busy_screen_and_pending_speech_do_not_bypass_recovery_in_run_loop(self):
        p, bridge = self.player(state(busy=True))
        p.DEATH_BUSY_TIMEOUT = 0
        bridge.screen.return_value = {"screen": "battle", "busy": True}
        p._pending_speech = "unfinished"
        p._wait_spoken = AsyncMock(side_effect=AssertionError("不应等待语音"))
        def end():
            p.stop()
            return {"ok": True}
        bridge.end_turn.side_effect = end
        await asyncio.wait_for(p.run_forever(0), 1)
        bridge.end_turn.assert_called_once()
        p._wait_spoken.assert_not_awaited()
        p._ask.assert_not_awaited()

    async def test_bridge_failure_does_not_ask_model_or_reset_retry_count(self):
        p, bridge = self.player()
        bridge.end_turn.side_effect = BridgeError("桥断开")
        result = await p.step()
        self.assertFalse(result["ok"])
        self.assertEqual(p._death_attempts, 1)
        p._ask.assert_not_awaited()


class BridgeLifeTests(unittest.TestCase):
    def test_grans_zero_hp_is_not_permanently_merged_as_busy(self):
        bridge = GameBridge()
        current, snap = grans_state(), grans_snapshot()
        current["busy"] = True
        current["battle"]["hp"] = 0
        snap.update(can_switch=False, busy=False, life={**life(), "turn": 2})
        bridge.call = Mock(return_value={"state": current})
        bridge._call_grans = Mock(return_value=snap)
        merged = bridge.state()
        self.assertFalse(merged["busy"])
        self.assertTrue(lifecycle.needs_recovery(merged))
        self.assertFalse(merged["battle"]["grans"]["can_switch"])

    def test_other_character_can_use_generic_death_interface(self):
        bridge = GameBridge()
        bridge.call = Mock(return_value={"state": state(busy=True)})
        bridge._call_grans = Mock(side_effect=[life(), life(hp=1)])
        self.assertFalse(bridge.state()["busy"])
        self.assertEqual(bridge.recover_death()["hp"], 1)
        self.assertEqual(bridge._call_grans.call_args.args, ("recover_death", {"battle_id": "battle1", "turn": 3}))

    def test_missing_extension_preserves_legacy_recovery_path(self):
        bridge = GameBridge()
        bridge.call = Mock(return_value={"state": state()})
        bridge._call_grans = Mock(side_effect=BridgeError("旧游戏未安装接口"))
        self.assertEqual(bridge.state(), state())

    def test_alive_or_finished_snapshot_does_not_infer_another_death(self):
        merged = lifecycle.merge_life(state(), life(hp=1))
        self.assertFalse(lifecycle.needs_recovery(merged))
        self.assertEqual(merged["run"]["hp"], 1)
        self.assertTrue(lifecycle.merge_life(state(), life(over=True))["battle"]["battle_over"])

    def test_stale_round_cannot_override_busy_or_hp(self):
        with self.assertRaises(ValueError):
            lifecycle.merge_life(state(), {**life(), "turn": 99})


if __name__ == "__main__": unittest.main()
