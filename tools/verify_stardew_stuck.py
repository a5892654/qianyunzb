# -*- coding: utf-8 -*-
"""星露谷卡死看门狗：花舞节这类过场卡住时，不能一直空耗直播。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dtgame import stardew  # noqa: E402
from dtgame.stardew import StardewPlayer  # noqa: E402


def make_player():
    pushes = []
    player = StardewPlayer({}, None, log_push=lambda level, msg: pushes.append((level, msg)))
    return player, pushes


class _FakeGame:
    def __init__(self, state, on_call=None):
        self.state_data = state
        self.calls = []
        self.on_call = on_call

    async def state(self):
        return dict(self.state_data)

    async def call(self, name, arguments=None, timeout=None):
        arguments = arguments or {}
        self.calls.append((name, arguments))
        if self.on_call:
            self.on_call(name, arguments)
        return {"text": f"{name} ok", "is_error": False, "data": {}, "raw": {}}

    async def restart_mcp(self):
        self.calls.append(("restart_mcp", {}))


class _TempGame(_FakeGame):
    async def call(self, name, arguments=None, timeout=None):
        if name == "travel_to":
            self.calls.append((name, arguments or {}))
            if self.on_call:
                self.on_call(name, arguments or {})
            return {"text": "Ended up in Temp instead of Forest.", "is_error": True, "data": {}, "raw": {}}
        return await super().call(name, arguments, timeout)


class _SlowGame(_FakeGame):
    async def call(self, name, arguments=None, timeout=None):
        if name == "travel_to":
            await asyncio.sleep(0.06)
        return await super().call(name, arguments, timeout)


class _ScriptedPlayer(StardewPlayer):
    def __init__(self, game, action, **kwargs):
        super().__init__({}, game, **kwargs)
        self.action = action
        self._tool_map = {
            "travel_to": {"name": "travel_to", "inputSchema": {
                "properties": {"location": {"type": "string"}}, "required": ["location"]}},
            "close_menu": {"name": "close_menu", "inputSchema": {"properties": {}, "required": []}},
            "festival": {"name": "festival", "inputSchema": {
                "properties": {"action": {"type": "string"}}, "required": ["action"]}},
            "go_to_bed": {"name": "go_to_bed", "inputSchema": {"properties": {}, "required": []}},
            "look": {"name": "look", "inputSchema": {"properties": {"radius": {"type": "integer"}}}},
        }

    async def _read_extras(self):
        return []

    async def _ask(self, user_text):
        return dict(self.action)

    async def _ai_situation_line(self, tool, args, *, in_progress=None):
        return "还在赶路，快到了。"


def test_playing_state_resets_timer():
    player, _ = make_player()
    state = {"day": 24, "time": "09:20", "location": "Forest"}
    assert player._stuck_action(state) is None
    player._stuck_at -= 481
    assert player._stuck_action({"day": 24, "time": "09:30", "location": "Forest"}) is None
    assert player._stuck_stage == 0, "时间在走就不算卡住"


def test_fishing_energy_change_resets_timer():
    player, _ = make_player()
    state = {"day": 24, "time": "12:00", "location": "Forest", "energy": 100, "money": 500}
    assert player._stuck_action(state) is None
    player._stuck_at -= 481
    assert player._stuck_action({**state, "energy": 92}) is None, "钓鱼掉体力是正常进展"
    assert player._stuck_stage == 0


def test_recovery_chain_then_stop():
    player, pushes = make_player()
    state = {"day": 24, "time": "09:20", "location": "Forest"}
    player._stuck_action(state)
    player._stuck_at -= 481
    action = player._stuck_action(state)
    assert action["tool"] == "close_menu", action
    player._stuck_at -= 481
    action = player._stuck_action(state)
    assert action["tool"] == "festival" and action["args"] == {"action": "leave"}, action
    player._stuck_at -= 481
    action = player._stuck_action(state)
    assert action["tool"] == "go_to_bed", action
    player._stuck_at -= 481
    assert player._stuck_action(state) is player._STOP
    assert not player.running, "卡死无法恢复时必须停止代打"
    assert any("卡住太久" in msg for _, msg in pushes), pushes


def test_other_location_goes_to_bed_first():
    player, _ = make_player()
    state = {"day": 24, "time": "12:00", "location": "FarmHouse"}
    player._stuck_action(state)
    player._stuck_at -= 481
    assert player._stuck_action(state)["tool"] == "close_menu"
    player._stuck_at -= 481
    assert player._stuck_action(state)["tool"] == "go_to_bed", "不在节日地图应直接回家睡觉"


def test_staged_recovery_escalates_quickly():
    player, _ = make_player()
    state = {"day": 24, "time": "09:20", "location": "Forest"}
    assert player._stuck_action(state) is None
    player._stuck_at -= player.STUCK_SECONDS + 1
    assert player._stuck_action(state)["tool"] == "close_menu"
    player._stuck_at -= player.STUCK_ESCALATE_SECONDS + 1
    assert player._stuck_action(state)["tool"] == "festival", "第一次恢复失败后应一分钟内升级，而不是再等整段卡死时间"


def test_fake_successful_travel_does_not_reset_watchdog():
    """第 24 天花舞节：travel_to 一直返回成功但人没离开 Forest，不能因此重置卡死计时。"""
    state = {"ready": True, "day": 24, "time": "09:20", "location": "Forest",
             "energy": 100, "money": 1397}
    holder = {}

    def on_call(name, _args):
        if name == "travel_to":
            holder["player"]._stuck_at -= 481

    game = _FakeGame(state, on_call)
    player = _ScriptedPlayer(
        game, {"tool": "travel_to", "args": {"location": "Forest"}, "say": ""},
        log_push=lambda level, msg: None,
    )
    holder["player"] = player
    asyncio.run(player.step())
    asyncio.run(player.step())
    assert game.calls[-1][0] == "close_menu", game.calls
    assert sum(1 for name, _ in game.calls if name == "travel_to") == 1, game.calls


def test_repeated_failures_trigger_recovery_before_full_timeout():
    player, _ = make_player()
    state = {"day": 24, "time": "09:20", "location": "Farm"}
    assert player._stuck_action(state) is None
    player._stuck_fails = player.FAIL_STUCK_LIMIT
    assert player._stuck_action(state)["tool"] == "close_menu", "连续失败十几次不能还等满整段卡死时间"


def test_not_ready_timeout_stops():
    pushes = []
    player = _ScriptedPlayer(
        _FakeGame({"ready": False}), {"tool": "look", "args": {}, "say": ""},
        log_push=lambda level, msg: pushes.append((level, msg)),
    )
    player.running = True
    player._not_ready_since -= player.READY_TIMEOUT + 1
    asyncio.run(player.step())
    assert not player.running, "一直进不了存档不能无限等"
    assert any("没有进入存档" in msg for _, msg in pushes), pushes


def test_stop_now_stops():
    pushes = []
    player = _ScriptedPlayer(
        _FakeGame({"ready": True, "stopNow": True}), {"tool": "look", "args": {}, "say": ""},
        log_push=lambda level, msg: pushes.append((level, msg)),
    )
    player.running = True
    asyncio.run(player.step())
    assert not player.running, "游戏侧要求停止时代打必须结束，不能空转"
    assert any("要求停止" in msg for _, msg in pushes), pushes


def test_temp_travel_is_treated_as_festival_success():
    state = {"ready": True, "day": 24, "time": "10:30", "location": "Temp",
             "energy": 100, "money": 1397, "festival": True, "eventUp": True}
    player = _ScriptedPlayer(
        _TempGame(state), {"tool": "travel_to", "args": {"location": "Forest"}, "say": "去花舞节"},
        narrate=False)
    asyncio.run(player.step())
    assert player._last_error == "", player._last_error
    assert "花舞节现场" in player._last_result
    assert player._stuck_fails == 0, "已经在节日现场不能把 Temp 当成失败去重试"


def test_long_action_gets_progress_commentary():
    state = {"ready": True, "day": 24, "time": "10:30", "location": "Farm",
             "energy": 100, "money": 1397}
    speaks = []
    player = _ScriptedPlayer(
        _SlowGame(state), {"tool": "travel_to", "args": {"location": "Forest"}, "say": "去森林"},
        speak=lambda text, emotion="": speaks.append(text), narrate=True)
    player.running = True
    player.LONG_ACTION_SPEAK_SECONDS = 0.01
    asyncio.run(player.step())
    assert speaks and speaks[0] == "去森林", speaks
    assert len(speaks) >= 2, f"长动作期间应补话，不能一直静音：{speaks}"


def test_prompt_and_catalog():
    player, _ = make_player()
    player._catalog = "- status()：查看状态"
    prompt = player._system_prompt()
    assert "禁止用 wait" in prompt, "提示词必须禁用 wait"
    assert "festival 只接受 start/leave/eggs" in prompt, "节日规则缺失"
    assert "Ended up in Temp instead of Forest" in prompt, "花舞节 Temp 误报规则缺失"
    assert "festival=true" in prompt, "节日现场禁止 travel_to 的规则缺失"
    assert "wait" not in stardew._TOP_TOOLS and "festival" in stardew._TOP_TOOLS


def main():
    tests = [test_playing_state_resets_timer, test_fishing_energy_change_resets_timer,
             test_recovery_chain_then_stop, test_other_location_goes_to_bed_first,
             test_staged_recovery_escalates_quickly, test_fake_successful_travel_does_not_reset_watchdog,
             test_repeated_failures_trigger_recovery_before_full_timeout,
             test_temp_travel_is_treated_as_festival_success,
             test_long_action_gets_progress_commentary,
             test_not_ready_timeout_stops, test_stop_now_stops, test_prompt_and_catalog]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"[PASS] {test.__name__}")
        except AssertionError as error:
            failed += 1
            print(f"[FAIL] {test.__name__} :: {error}")
        except Exception as error:
            failed += 1
            print(f"[FAIL] {test.__name__} :: {type(error).__name__}: {error}")
    print(f"合计 {len(tests)} 项，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
