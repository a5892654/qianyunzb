"""杀戮尖塔2接入回归：决策参数、菜单启动、结束重开、端口发现；不连真实游戏。"""
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from dtgame.sts2 import STS2Player, STS2Process, _parse_hp, discover_port


class FakeGame:
    def __init__(self, decide_payload=None, available_actions=None):
        self.decide_payload = decide_payload
        self.available_actions = available_actions or []
        self.calls = []

    async def call(self, name, arguments=None, timeout=None):
        self.calls.append((name, dict(arguments or {})))
        if name == "decide":
            return {"text": json.dumps(self.decide_payload or {}), "is_error": False,
                    "data": {}, "raw": {}}
        if name == "get_available_actions":
            return {"text": json.dumps(self.available_actions), "is_error": False,
                    "data": {}, "raw": {}}
        return {"text": "ok", "is_error": False, "data": {}, "raw": {}}


DECIDE = {
    "state": {
        "screen": "COMBAT", "in_combat": True, "turn": 2, "run_id": "r1",
        "available_actions": ["play_card", "end_turn"],
        "combat": {
            "player": {"hp": "70/80", "block": 0, "energy": 3},
            "players": [{"player_id": "1"}],
            "enemies": [{"index": 0, "current_hp": 20, "intent": "ATTACK"}],
            "hand": [{"index": 0, "card_id": "STRIKE_IRONCLAD", "name": "打击",
                      "playable": True, "energy_cost": 1}],
        },
        "run": {"character": "铁甲战士", "floor": 1, "hp": "70/80", "gold": 99,
                "deck": [{"line": "打击*5"}], "players": [{"player_id": "1"}]},
    },
    "available_actions": [
        {"name": "play_card", "requires_target": True, "requires_index": True},
        {"name": "end_turn", "requires_target": False, "requires_index": False},
    ],
    "scene_guidance": "fight the enemy",
}


class ActionTests(unittest.TestCase):
    def test_play_card_aliases(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        actions = {"play_card": {"requires_target": True, "requires_index": True}}
        self.assertEqual(
            p._action_args({"action": "play_card", "index": 1, "target": 0}, actions),
            ("play_card", {"action": "play_card", "card_index": 1, "target_index": 0}),
        )

    def test_menu_index_maps_to_option(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        actions = {"choose_map_node": {"requires_index": True}}
        self.assertEqual(
            p._action_args({"action": "choose_map_node", "index": 2}, actions),
            ("choose_map_node", {"action": "choose_map_node", "option_index": 2}),
        )

    def test_unwraps_act_envelope(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        actions = {"end_turn": {}}
        self.assertEqual(p._action_args({"tool": "act", "args": {"action": "end_turn"}}, actions),
                         ("end_turn", {"action": "end_turn"}))

    def test_invalid_action_rejected(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        with self.assertRaises(ValueError):
            p._action_args({"action": "made_up"}, {"end_turn": {}})

    def test_play_card_requires_index(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        with self.assertRaises(ValueError):
            p._action_args({"action": "play_card"}, {"play_card": {"requires_index": True}})

    def test_blocked_actions_are_removed(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        usable = p._usable_actions({"save_and_quit": {}, "end_turn": {}, "return_to_menu": {}})
        self.assertEqual(list(usable), ["end_turn"])

    def test_missing_target_defaults_to_first_enemy(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        name, args = p._action_args({"action": "play_card", "card_index": 0},
                                    {"play_card": {"requires_target": True}})
        self.assertEqual((name, args["target_index"]), ("play_card", 0))


class StateTests(unittest.TestCase):
    def test_prompt_state_drops_duplicate_players(self):
        payload = STS2Player._prompt_state(DECIDE["state"])
        data = json.loads(payload)
        self.assertNotIn("players", data["combat"])
        self.assertNotIn("players", data["run"])
        self.assertEqual(data["screen"], "COMBAT")

    def test_hp_parser(self):
        self.assertEqual(_parse_hp("73/80"), (73, 80))
        self.assertEqual(_parse_hp("?"), (None, None))

    def test_watchdog_auto_selects_stuck_card(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        p.STALL_CARD_SECONDS = 0
        state = {"screen": "CARD_SELECTION", "selection": {"kind": "combat_hand_select"},
                 "combat": {}}
        actions = {"select_deck_card": {"requires_index": True}}
        self.assertIsNone(p._watchdog("CARD_SELECTION", state, actions))
        self.assertEqual(p._watchdog("CARD_SELECTION", state, actions),
                         ("select_deck_card", {"option_index": 0}))

    def test_watchdog_recovers_stuck_enemy_turn(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        p.STALL_COMBAT_SECONDS = 0
        state = {"screen": "COMBAT", "in_combat": True,
                 "combat": {"action_readiness": {"can_use_combat_actions": False},
                            "player": {"hp": "26/75"}}}
        actions = {"save_and_quit": {}, "discard_potion": {}}
        self.assertIsNone(p._watchdog("COMBAT", state, actions))
        self.assertEqual(p._watchdog("COMBAT", state, actions), ("save_and_quit", {}))

    def test_watchdog_does_not_reset_active_enemy_animation(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        p.STALL_COMBAT_SECONDS = 0
        state = {"screen": "COMBAT", "in_combat": True,
                 "combat": {"action_readiness": {"can_use_combat_actions": False,
                                                 "ending_turn_phase_one": True},
                            "player": {"hp": "26/75"}}}
        actions = {"save_and_quit": {}}
        p._watchdog("COMBAT", state, actions)
        self.assertIsNone(p._watchdog("COMBAT", state, actions))

    def test_watchdog_confirms_stuck_modal(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        p.STALL_MODAL_SECONDS = 0
        state = {"screen": "MODAL", "combat": {}}
        actions = {"confirm_modal": {}}
        self.assertIsNone(p._watchdog("MODAL", state, actions))
        self.assertEqual(p._watchdog("MODAL", state, actions), ("confirm_modal", {}))

    def test_result_pending(self):
        self.assertTrue(STS2Player._result_pending('{"stable":true,"previous":{"stable":false}}'))
        self.assertTrue(STS2Player._result_pending('{"status":"pending"}'))
        self.assertFalse(STS2Player._result_pending('{"stable":true}'))

    def test_has_playable_cards(self):
        self.assertTrue(STS2Player._has_playable_cards(DECIDE["state"]))
        empty = copy.deepcopy(DECIDE["state"])
        empty["combat"]["player"]["energy"] = 0
        self.assertFalse(STS2Player._has_playable_cards(empty))


class NarrationTests(unittest.TestCase):
    def test_generic_energy_line_is_filtered(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        self.assertEqual(p._clean_line("先把能量用掉。"), "")
        self.assertEqual(p._clean_line("先把能量用掉"), "")

    def test_skips_mid_combo_then_speaks_summary(self):
        says = []
        p = STS2Player({}, FakeGame(), speak=lambda text, emotion="": says.append(text), narrate=True)
        p._battle_opening = False
        state = {"screen": "COMBAT", "in_combat": True, "combat": {"player": {"hp": "70/80"}}}
        p._combo_target = 2
        p._roll_narration_target = lambda: 2
        p._silent_actions = ["play_card"]
        self.assertEqual(p._maybe_speak("play_card", state, "打这张。", ""), "")
        p._silent_actions = ["play_card", "play_card"]
        self.assertEqual(p._maybe_speak("play_card", state, "连招总结。", ""), "连招总结。")
        self.assertEqual(says, ["连招总结。"])

    def test_every_step_mode_speaks_each_action(self):
        says = []
        p = STS2Player({}, FakeGame(), speak=lambda text, emotion="": says.append(text), narrate=True)
        p._battle_opening = False
        p._combo_target = 1
        p._roll_narration_target = lambda: 1
        state = {"screen": "COMBAT", "in_combat": True, "combat": {"player": {"hp": "70/80"}}}
        p._silent_actions = ["play_card"]
        self.assertEqual(p._maybe_speak("play_card", state, "第一张。", ""), "第一张。")
        p._silent_actions = ["play_card"]
        self.assertEqual(p._maybe_speak("play_card", state, "第二张。", ""), "第二张。")
        self.assertEqual(says, ["第一张。", "第二张。"])

    def test_roll_narration_target_is_one_or_two_silent_steps(self):
        with patch("dtgame.sts2.random.choice", return_value=2):
            self.assertEqual(STS2Player._roll_narration_target(), 2)
        with patch("dtgame.sts2.random.choice", return_value=3):
            self.assertEqual(STS2Player._roll_narration_target(), 3)

    def test_target_three_skips_two_steps(self):
        says = []
        p = STS2Player({}, FakeGame(), speak=lambda text, emotion="": says.append(text), narrate=True)
        p._battle_opening = False
        p._combo_target = 3
        p._roll_narration_target = lambda: 3
        state = {"screen": "COMBAT", "in_combat": True, "combat": {"player": {"hp": "70/80"}}}
        p._silent_actions = ["a", "b"]
        self.assertEqual(p._maybe_speak("play_card", state, "第三手总结。", ""), "")
        p._silent_actions = ["a", "b", "c"]
        self.assertEqual(p._maybe_speak("play_card", state, "第三手总结。", ""), "第三手总结。")

    def test_prefers_narrate_fresh_when_pipe_supports_it(self):
        class FakePipe:
            def __init__(self):
                self.calls = []

            def narrate_fresh(self, text, emotion=""):
                self.calls.append((text, emotion))

        pipe = FakePipe()
        p = STS2Player({}, FakeGame(), narrate=True, pipe=pipe)
        p._battle_opening = False
        p._combo_target = 1
        p._roll_narration_target = lambda: 1
        state = {"screen": "COMBAT", "in_combat": True, "combat": {"player": {"hp": "70/80"}}}
        p._silent_actions = ["play_card"]
        p._maybe_speak("play_card", state, "这刀先压住它。", "开心")
        self.assertEqual(pipe.calls, [("这刀先压住它。", "开心")])

    def test_no_say_stays_silent(self):
        says = []
        p = STS2Player({}, FakeGame(), speak=lambda text, emotion="": says.append(text), narrate=True)
        p._battle_opening = False
        p._quiet_from = time.monotonic() - 20
        state = {"screen": "COMBAT", "in_combat": True, "combat": {"player": {"hp": "70/80"}}}
        self.assertEqual(p._maybe_speak("play_card", state, "", ""), "")
        self.assertEqual(says, [])

    def test_fresh_commentary_uses_cached_ai_line(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        p._last_state = {"screen": "COMBAT", "in_combat": True, "combat": {}}
        p._state_time = time.monotonic()
        p._fresh_line = "这波先稳住，别急着交回合。"
        p._fresh_at = time.monotonic()
        self.assertEqual(p.fresh_commentary(), "这波先稳住，别急着交回合。")
        p._fresh_at = time.monotonic() - 30
        self.assertEqual(p.fresh_commentary(), "")

    def test_fresh_commentary_never_reads_state_numbers(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        p._last_state = {
            "screen": "COMBAT", "in_combat": True,
            "combat": {"player": {"current_hp": 22, "max_hp": 70, "energy": 1},
                       "enemies": [{"name": "墨宝", "current_hp": 14}]},
        }
        p._state_time = time.monotonic()
        self.assertEqual(p.fresh_commentary(), "")

    def test_fresh_commentary_expires(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        p._last_state = {"screen": "COMBAT", "in_combat": True, "combat": {}}
        p._state_time = time.monotonic() - 30
        self.assertEqual(p.fresh_commentary(), "")

    def test_low_hp_only_fires_on_threshold_crossing(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        state = {"screen": "COMBAT", "in_combat": True, "combat": {"player": {"hp": "20/80"}}}
        self.assertTrue(p._important(state, "play_card"))
        self.assertFalse(p._important(state, "play_card"))
        p._low_hp = False
        p._important(state, "play_card")
        healthy = {"screen": "COMBAT", "in_combat": True, "combat": {"player": {"hp": "70/80"}}}
        self.assertFalse(p._important(healthy, "play_card"))
        self.assertTrue(p._important(state, "play_card"))

    def test_important_action_speaks_immediately(self):
        says = []
        p = STS2Player({}, FakeGame(), speak=lambda text, emotion="": says.append(text), narrate=True)
        state = {"screen": "REWARD", "in_combat": False}
        p._silent_actions = ["play_card"]
        line = p._maybe_speak("choose_reward_card", state, "拿这张。", "")
        self.assertEqual(line, "拿这张。")
        self.assertEqual(says, ["拿这张。"])


class PlayerFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_step_calls_act_with_say(self):
        says = []
        game = FakeGame(DECIDE)
        p = STS2Player({"timeout": 10, "max_tokens": 100}, game,
                       speak=lambda text, emotion="": says.append((text, emotion)), narrate=True)
        p._ask = AsyncMock(return_value={"action": "play_card", "card_index": 0,
                                         "target_index": 0, "say": "打这张。", "emotion": "开心"})
        await p.step()
        self.assertEqual(says, [("打这张。", "开心")])
        self.assertEqual(p.turns_taken, 1)
        name, args = game.calls[-1]
        self.assertEqual(name, "act")
        self.assertEqual(args["action"], "play_card")
        self.assertEqual(args["card_index"], 0)
        self.assertEqual(args["reason"], "打这张。")

    async def test_boot_continues_save(self):
        game = FakeGame()
        p = STS2Player({}, game, narrate=False)
        await p._boot({"continue_run": {"requires_index": False}})
        self.assertEqual(game.calls[-1][1]["action"], "continue_run")
        self.assertFalse(p._continue_failed)

    async def test_boot_starts_new_when_requested(self):
        game = FakeGame()
        p = STS2Player({}, game, narrate=False, new_run=True)
        await p._boot({"continue_run": {}, "abandon_run": {}})
        self.assertEqual(game.calls[-1][1]["action"], "abandon_run")
        self.assertFalse(p.new_run)

    async def test_main_menu_actions_survive_to_boot(self):
        payload = {"state": {"screen": "MAIN_MENU", "available_actions": ["open_character_select"]},
                   "available_actions": [{"name": "open_character_select"}],
                   "scene_guidance": ""}
        game = FakeGame(payload)
        p = STS2Player({}, game, narrate=False)
        await p.step()
        self.assertEqual(game.calls[-1][1]["action"], "open_character_select")

    async def test_game_over_actions_survive_to_handler(self):
        payload = {"state": {"screen": "GAME_OVER", "run_id": "r1",
                             "run": {"character": "铁甲战士", "floor": 3},
                             "available_actions": ["return_to_main_menu"]},
                   "available_actions": [{"name": "return_to_main_menu"}],
                   "scene_guidance": ""}
        game = FakeGame(payload)
        p = STS2Player({}, game, narrate=False)
        p.GAME_OVER_PAUSE = 0
        await p.step()
        self.assertEqual(game.calls[-1][1]["action"], "return_to_main_menu")

    async def test_boot_falls_back_to_character_select(self):
        game = FakeGame()
        p = STS2Player({}, game, narrate=False)
        await p._boot({"open_character_select": {}})
        self.assertEqual(game.calls[-1][1]["action"], "open_character_select")

    async def test_game_over_continues_native_summary(self):
        game = FakeGame()
        p = STS2Player({}, game, narrate=False)
        p.GAME_OVER_PAUSE = 0
        await p._game_over({"run_id": "r1", "run": {"character": "铁甲战士", "floor": 3}},
                           {"continue_game_over": {}})
        self.assertEqual(game.calls[-1][1]["action"], "continue_game_over")
        self.assertTrue(p._continue_failed)

    async def test_game_over_returns_to_menu(self):
        game = FakeGame()
        p = STS2Player({}, game, narrate=False)
        p.GAME_OVER_PAUSE = 0
        p._game_over_run = "r1"
        await p._game_over({"run_id": "r1", "run": {"character": "铁甲战士", "floor": 3}},
                           {"return_to_main_menu": {}})
        self.assertEqual(game.calls[-1][1]["action"], "return_to_main_menu")

    async def test_game_over_waits_when_no_actions(self):
        game = FakeGame()
        p = STS2Player({}, game, narrate=False)
        p.GAME_OVER_PAUSE = 0
        p._game_over_run = "r1"
        await p._game_over({"run_id": "r1", "run": {"character": "铁甲战士", "floor": 3}}, {})
        self.assertEqual(game.calls[-1][0], "wait_until_actionable")

    async def test_card_selection_ignores_stale_play_card(self):
        payload = {
            "state": {"screen": "CARD_SELECTION", "in_combat": True, "turn": 2,
                      "selection": {"kind": "combat_hand_select",
                                    "cards": [{"index": 0}, {"index": 1}]},
                      "available_actions": ["play_card", "select_deck_card"]},
            "available_actions": [{"name": "play_card"},
                                  {"name": "select_deck_card", "requires_index": True}],
            "scene_guidance": "",
        }
        game = FakeGame(payload)
        p = STS2Player({}, game, narrate=False)
        p._ask = AsyncMock(return_value={"action": "select_deck_card", "option_index": 1,
                                         "say": "弃这张。"})
        await p.step()
        self.assertEqual(game.calls[-1][1]["action"], "select_deck_card")
        self.assertEqual(game.calls[-1][1]["option_index"], 1)

    async def test_card_selection_refreshes_stale_actions(self):
        payload = {
            "state": {"screen": "CARD_SELECTION", "in_combat": True,
                      "selection": {"kind": "combat_hand_select", "cards": [{"index": 0}]},
                      "available_actions": ["play_card"]},
            "available_actions": [{"name": "play_card"}],
            "scene_guidance": "",
        }
        game = FakeGame(payload, available_actions=[{"name": "select_deck_card", "requires_index": True}])
        p = STS2Player({}, game, narrate=False)
        p._ask = AsyncMock(return_value={"action": "select_deck_card", "option_index": 0,
                                         "say": "弃这张。"})
        await p.step()
        self.assertEqual(game.calls[-1][1]["action"], "select_deck_card")

    async def test_card_selection_waits_when_refresh_empty(self):
        payload = {
            "state": {"screen": "CARD_SELECTION", "in_combat": True,
                      "selection": {"kind": "combat_hand_select", "cards": []},
                      "available_actions": ["play_card"]},
            "available_actions": [{"name": "play_card"}],
            "scene_guidance": "",
        }
        game = FakeGame(payload, available_actions=[])
        p = STS2Player({}, game, narrate=False)
        p._ask = AsyncMock()
        await p.step()
        self.assertEqual(game.calls[-1][0], "wait_until_actionable")
        p._ask.assert_not_awaited()

    async def test_empty_actions_waits(self):
        game = FakeGame({"state": {"screen": "MAP", "available_actions": []},
                         "available_actions": [], "scene_guidance": ""})
        p = STS2Player({}, game, narrate=False)
        await p.step()
        self.assertEqual(game.calls[-1][0], "wait_until_actionable")

    async def test_auto_plays_when_model_keeps_ending_turn(self):
        game = FakeGame(DECIDE)
        p = STS2Player({}, game, narrate=False)
        p._ask = AsyncMock(return_value={"action": "end_turn", "say": "能量空了。"})
        await p.step()
        self.assertEqual(game.calls[-1][1]["action"], "play_card")
        self.assertEqual(game.calls[-1][1]["card_index"], 0)

    async def test_ai_situation_line_rejects_status_readout(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        p._last_state = {"screen": "COMBAT", "in_combat": True, "combat": {}}
        p._state_time = time.monotonic()
        p._complete = AsyncMock(return_value="现在0能量，血22/70，敌人还剩14血。")
        self.assertEqual(await p._ai_situation_line(), "")

    async def test_ai_situation_line_accepts_natural_line(self):
        p = STS2Player({}, FakeGame(), narrate=False)
        p._last_state = {"screen": "COMBAT", "in_combat": True, "combat": {}}
        p._state_time = time.monotonic()
        p._complete = AsyncMock(return_value="这蘑菇还想叠易伤，先把它按死。")
        self.assertEqual(await p._ai_situation_line(), "这蘑菇还想叠易伤，先把它按死。")

    async def test_end_turn_waits_for_enemy_settle(self):
        payload = copy.deepcopy(DECIDE)
        payload["state"]["combat"]["player"]["energy"] = 0
        game = FakeGame(payload)
        p = STS2Player({}, game, narrate=False)
        p._ask = AsyncMock(return_value={"action": "end_turn", "say": "结束回合。"})
        await p.step()
        self.assertEqual(game.calls[-2][0], "act")
        self.assertEqual(game.calls[-1][0], "wait_until_actionable")

    async def test_enemy_animation_waits_instead_of_ending_turn(self):
        payload = copy.deepcopy(DECIDE)
        payload["state"]["combat"]["action_readiness"] = {
            "can_use_combat_actions": False, "modal_open": False}
        payload["available_actions"] = [{"name": "end_turn"}]
        game = FakeGame(payload)
        p = STS2Player({}, game, narrate=False)
        p._ask = AsyncMock()
        await p.step()
        self.assertEqual(game.calls[-1][0], "wait_until_actionable")
        p._ask.assert_not_awaited()


class ProcessTests(unittest.TestCase):
    def test_exe_default(self):
        p = STS2Process({"game_dir": r"C:\game\sljt"})
        self.assertTrue(p.exe().endswith("SlayTheSpire2.exe"))

    def test_discover_port_scans(self):
        with patch("dtgame.sts2._port_alive", side_effect=lambda host, port: port == 8082):
            self.assertEqual(discover_port("127.0.0.1", 8080), 8082)

    def test_config_has_sts2_defaults(self):
        self.assertIn("sts2", config.DEFAULTS)
        self.assertIn("game_dir", config.DEFAULTS["sts2"])
        self.assertEqual(config.DEFAULTS["broadcast"]["game_titles"]["sts2"], "大胖鱼开播了，来一起爬塔")


if __name__ == "__main__":
    unittest.main()
