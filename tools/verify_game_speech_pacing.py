"""代打语音节奏回归：操作不等语音、空台词不硬凑、攒几步再连招总结。"""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dtgame.player import COMBO_STEPS, SILENCE_SECONDS, Player


def state(hp=50, turn=1):
    return {"screen": "battle",
            "run": {"run_id": "run1", "character": "奈尔", "hp": hp, "max_hp": 60, "mode": "deep"},
            "battle": {"battle_id": "fight1", "hp": hp, "max_hp": 60, "turn": turn,
                       "enemies": [{"id": "slime", "hp": 20}]}}


def make_player(speak=None, pipe=None):
    bridge = Mock()
    bridge.state.return_value = state()
    bridge.screen.return_value = {"screen": "battle"}
    bridge.end_turn.return_value = {"ok": True}
    player = Player({}, bridge, speak=speak or Mock(return_value=None), pipe=pipe)
    player._ask = AsyncMock(return_value={"action": "end", "say": ""})
    player.log_push = Mock()
    player.running = True
    return player, bridge


class SpeechPacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_loop_never_waits_for_unfinished_speech(self):
        pipe = Mock()
        pipe.spoken_done.return_value = False
        speak = Mock(return_value=1)
        player, bridge = make_player(speak=speak, pipe=pipe)
        player._ask = AsyncMock(return_value={"action": "end", "say": "先稳住。"})
        def end():
            if bridge.end_turn.call_count >= 3:
                player.stop()
            return {"ok": True}
        bridge.end_turn.side_effect = end
        await asyncio.wait_for(player.run_forever(0), 1)
        self.assertEqual(bridge.end_turn.call_count, 3)
        self.assertGreaterEqual(speak.call_count, 1)

    async def test_battle_opening_then_alternating_single_lines(self):
        speak = Mock(return_value=None)
        player, _ = make_player(speak=speak)
        lines = ["开场先来一句。", "第二手接上。", "第三手也接上。", "第四手收尾。"]
        player._ask = AsyncMock(side_effect=[{"action": "end", "say": line} for line in lines])
        for turn in range(1, 5):
            await player.step(state(turn=turn))
        self.assertEqual(speak.call_count, 2)
        self.assertEqual(speak.call_args_list[0].args[0], lines[0])
        self.assertEqual(speak.call_args_list[1].args[0], lines[2])

    async def test_silent_steps_then_combo_summary(self):
        speak = Mock(return_value=None)
        player, _ = make_player(speak=speak)
        lines = ["开场先来一句。", "", "", "这两下连得顺，收尾。"]
        player._ask = AsyncMock(side_effect=[{"action": "end", "say": line} for line in lines])
        for turn in range(1, 5):
            await player.step(state(turn=turn))
        self.assertEqual(speak.call_count, 2)
        self.assertEqual(speak.call_args_list[1].args[0], lines[3])
        prompt = player._ask.await_args.args[0]
        self.assertIn("【连招解说】", prompt)
        self.assertIn('"action": "end"', prompt)

    async def test_battle_opening_speaks_even_after_a_recent_line(self):
        speak = Mock(return_value=None)
        player, _ = make_player(speak=speak)
        player.commentary.recent.append("上一场刚打完。")
        player._ask = AsyncMock(return_value={"action": "end", "say": "来，开打！"})
        result = await player.step(state())
        self.assertEqual(result["say"], "来，开打！")
        speak.assert_called_once_with("来，开打！", "")

    async def test_next_battle_gets_its_own_opening_line(self):
        speak = Mock(return_value=None)
        player, _ = make_player(speak=speak)
        player._ask = AsyncMock(side_effect=[{"action": "end", "say": "第一场开场。"},
                                             {"action": "end", "say": "下一场也不废话。"}])
        await player.step(state())
        second = state()
        second["battle"]["battle_id"] = "fight2"
        await player.step(second)
        self.assertEqual(speak.call_count, 2)
        self.assertEqual(speak.call_args_list[1].args[0], "下一场也不废话。")

    async def test_long_silence_forces_a_contextual_fallback_line(self):
        speak = Mock(return_value=None)
        player, _ = make_player(speak=speak)
        player._ask = AsyncMock(side_effect=[{"action": "end", "say": "开场。"},
                                             {"action": "end", "say": ""}])
        await player.step(state(turn=1))
        player._quiet_from = time.monotonic() - SILENCE_SECONDS - 1
        second = state(turn=2)
        second["battle"]["energy"] = 0
        result = await player.step(second)
        self.assertEqual(speak.call_count, 2)
        self.assertIn("能量", result["say"])
        self.assertLess(player._silence_seconds(), 1)

    async def test_long_silence_lets_a_model_line_bypass_the_combo_gate(self):
        speak = Mock(return_value=None)
        player, _ = make_player(speak=speak)
        player._ask = AsyncMock(side_effect=[{"action": "end", "say": "开场。"},
                                             {"action": "end", "say": "这口气不能断。"}])
        await player.step(state(turn=1))
        player._quiet_from = time.monotonic() - SILENCE_SECONDS - 1
        await player.step(state(turn=2))
        self.assertEqual(speak.call_count, 2)
        self.assertEqual(speak.call_args_list[1].args[0], "这口气不能断。")

    async def test_empty_say_does_not_invent_filler_every_step(self):
        speak = Mock(return_value=None)
        player, _ = make_player(speak=speak)
        for turn in range(1, 3):
            result = await player.step(state(turn=turn))
            self.assertEqual(result["say"], "")
        speak.assert_not_called()
        self.assertEqual(player._silent_actions, ['battle {"action": "end"}'] * 2)

    async def test_important_moment_speaks_without_waiting_for_the_combo(self):
        speak = Mock(return_value=None)
        player, _ = make_player(speak=speak)
        player._ask = AsyncMock(side_effect=[{"action": "end", "say": "开局先看看。"},
                                             {"action": "end", "say": "这一下有点疼。"}])
        await player.step(state())
        result = await player.step(state(20, turn=2))
        self.assertEqual(result["say"], "这一下有点疼。")
        self.assertEqual(speak.call_count, 2)
        self.assertEqual(speak.call_args_list[1].args[0], "这一下有点疼。")

    async def test_no_filler_before_the_silence_timeout(self):
        speak = Mock(return_value=None)
        player, _ = make_player(speak=speak)
        for turn in range(1, 6):
            result = await player.step(state(turn=turn))
            self.assertEqual(result["say"], "")
        speak.assert_not_called()
        self.assertEqual(len(player._silent_actions), 5)

    def test_context_line_uses_current_battle_facts(self):
        player, _ = make_player()
        self.assertIn("15", player.commentary.context_line(state(15)))
        no_energy = state(50)
        no_energy["battle"]["energy"] = 0
        self.assertIn("能量", player.commentary.context_line(no_energy))
        hand = state(50)
        hand["battle"]["hand"] = [{"name": "斩击"}]
        self.assertIn("1 张", player.commentary.context_line(hand))

    def test_prompts_allow_empty_say_and_ask_for_a_combo_summary(self):
        player, _ = make_player()
        self.assertIn("留空", player._system_prompt())
        player._battle_opening = True
        self.assertIn("【战斗开场】", player._combo_prompt(state()))
        player._battle_opening = False
        player._quiet_from = time.monotonic() - SILENCE_SECONDS - 1
        cold = player._combo_prompt(state())
        self.assertIn("【该开口了】", cold)
        self.assertIn("当前局面", cold)
        player._quiet_from = time.monotonic()
        player._silent_actions = ['battle {"action": "end"}'] * COMBO_STEPS
        prompt = player._combo_prompt(state())
        self.assertIn("【连招解说】", prompt)
        self.assertIn('"action": "end"', prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
