import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dtgame.bridge import GameBridge
from dtgame.mcp_server import Server, TOOLS
from dtgame.player import Player, SYSTEM
from dtgame import view
from tools import node_regression


def panel_state(screen="event", window="one", revision="v1"):
    return {"screen": screen, "window_id": window, screen: {
        "panel": "测试窗口", "window_id": window, "revision": revision,
        "options": [{"slot": 0, "name": "领取", "locked": False}],
        "buttons": [{"slot": 0, "text": "确认"}], "texts": ["窗口正文"], "can_back": False}}


def boss_dialog_state(window="boss1", revision="r1", panel="Panel"):
    return {"screen": "dialog", "window_id": window, "dialog": {
        "panel": panel, "window_id": window, "revision": revision, "options": [],
        "buttons": [
            {"slot": 0, "text": "◆ 兑换遗物 / 从Boss专属遗物中随机获得1件"},
            {"slot": 1, "text": "◆ 兑换传说卡牌 / 从传说卡牌中选择1张加入牌组"},
            {"slot": 2, "text": "◆ 捏碎魂核 / 直接获得 200 灵魂"},
            {"slot": 3, "text": "直接返回地图"}],
        "texts": ["战斗胜利", "◆ 兑换遗物"], "can_back": False}}


def battle_state(estus=None, potions=None, hp=20, max_hp=40):
    """战斗假状态：只填渲染要用的字段，estus/potions 按桥的新契约给。"""
    battle = {"turn": 3, "energy": 1, "max_energy": 3, "hp": hp, "max_hp": max_hp,
              "enemies": [{"name": "灰烬骑士", "hp": 30, "max_hp": 30,
                           "intent": {"type": "attack", "name": "重劈", "value": 12, "hits": 1}}],
              "hand": [{"slot": 0, "name": "横斩", "cost": 1, "type": "攻击",
                        "target": "single", "desc": "造成 8 点伤害", "blocked": ""}],
              "radiance": 5, "ultimate_cost": 20,
              "draw_pile_count": 9, "discard_pile_count": 3, "exhaust_pile_count": 0}
    if estus is not None:
        battle["estus"] = estus
    if potions is not None:
        battle["potions"] = potions
    return {"screen": "battle", "battle": battle,
            "run": {"character": "灰烬骑士", "hp": hp, "max_hp": max_hp, "souls": 120}}


def campfire_state():
    return {"screen": "campfire", "campfire": {"options": [
        {"slot": 0, "text": "休息：回复 30% 生命", "enabled": True, "action": True},
        {"slot": 1, "text": "灵魂锻造：升级一张卡", "enabled": True, "action": True},
        {"slot": 2, "text": "继续前行", "enabled": True, "action": False}]}}


def soul_forge_state(**over):
    body = {"window_id": "forge1", "revision": "f1", "souls": 150, "free": False,
            "cards": [{"slot": 0, "id": "slash", "name": "横斩", "cost": 1, "type": "攻击", "forge_count": 0},
                      {"slot": 1, "id": "guard", "name": "铁壁", "cost": 2, "type": "技能", "forge_count": 1}],
            "selected": {"slot": 0, "id": "slash", "name": "横斩"},
            "options": [{"slot": 0, "name": "强化伤害", "cost": 80, "affordable": True},
                        {"slot": 1, "name": "附加格挡", "cost": 300, "affordable": False}],
            "selected_option": 0, "can_forge": True}
    body.update(over)
    return {"screen": "soul_forge", "window_id": "forge1", "soul_forge": body}


def run_end_state(**over):
    """本局结算页（VictoryOverlay）：普通窗口正文 + finished / hall_button / can_back。
    「返回大厅」在危险词名单里，所以不会出现在 buttons 中。"""
    body = {"panel": "VictoryOverlay", "window_id": "win1", "revision": "v1",
            "title": "恶魔之塔 · 征服", "desc": "", "options": [], "buttons": [],
            "texts": ["第 3 幕 通关用时 51 分钟", "击败 BOSS：熔核巨像"],
            "can_back": True, "finished": True, "hall_button": {"text": "返回大厅"}}
    body.update(over)
    return {"screen": "run_end", "window_id": "win1", "run_end": body,
            "run": {"character": "灰烬骑士", "hp": 41, "max_hp": 60, "souls": 480}}


class ContractTests(unittest.TestCase):
    def test_schema_requires_window_tokens(self):
        tools = {tool["name"]: tool for tool in TOOLS}
        for name in ("event_choose", "overlay_action"):
            self.assertIn("window_id", tools[name]["inputSchema"]["required"])
            self.assertIn("revision", tools[name]["inputSchema"]["required"])

    def test_bridge_forwards_tokens(self):
        bridge = GameBridge()
        bridge.call = Mock(return_value={"ok": True})
        bridge.event(0, "one", "v1")
        bridge.call.assert_called_once_with("event", {"slot": 0, "window_id": "one", "revision": "v1"})
        bridge.call.reset_mock()
        bridge.overlay(2, "button", "two", "v2")
        bridge.call.assert_called_once_with("overlay", {"slot": 2, "op": "button", "window_id": "two", "revision": "v2"})

    def test_bridge_forwards_death_analysis(self):
        bridge = GameBridge()
        bridge.call = Mock(return_value={"ok": True})
        bridge.confirm_death_analysis()
        bridge.call.assert_called_once_with("confirm_death_analysis", timeout=20)

    def test_bridge_forwards_return_to_hall(self):
        bridge = GameBridge()
        bridge.call = Mock(return_value={"ok": True, "closed": True, "screen_after": "hall", "error": ""})
        self.assertEqual(bridge.return_to_hall()["screen_after"], "hall")
        bridge.call.assert_called_once_with("return_to_hall", timeout=40)

    def test_bridge_forwards_estus_and_soul_forge(self):
        bridge = GameBridge()
        bridge.call = Mock(return_value={"ok": True})
        bridge.use_estus()
        bridge.call.assert_called_once_with("use_estus", {"ash": False})
        bridge.call.reset_mock()
        bridge.use_estus(True)
        bridge.call.assert_called_once_with("use_estus", {"ash": True})
        bridge.call.reset_mock()
        bridge.soul_forge("card", 2)
        bridge.call.assert_called_once_with("soul_forge", {"op": "card", "slot": 2}, timeout=60)

    def test_view_battle_explains_estus_before_model_can_use_it(self):
        text = view.render(battle_state(estus={"uses": 1, "max": 2, "ash_uses": 0, "ash_max": 1,
                                               "can_use": True, "can_use_ash": True}))
        self.assertIn("元素瓶 1/2（可喝，不消耗回合，action=estus）", text)
        self.assertIn("灰元素瓶 0/1", text)
        self.assertIn("补光焰", text)
        self.assertIn("ash=true", text)

    def test_view_battle_says_why_estus_is_unusable(self):
        full = view.render(battle_state(hp=40, max_hp=40, estus={"uses": 2, "max": 2, "ash_uses": 0,
                                                                "ash_max": 0, "can_use": False,
                                                                "can_use_ash": False}))
        self.assertIn("元素瓶 2/2（血已满）", full)
        self.assertNotIn("灰元素瓶", full)  # ash_max=0 时不占行
        dry = view.render(battle_state(estus={"uses": 0, "max": 2, "ash_uses": 0, "ash_max": 0,
                                              "can_use": False, "can_use_ash": False}))
        self.assertIn("元素瓶 0/2（没次数了）", dry)
        self.assertIn("没次数了", view.render(battle_state(hp=40, max_hp=40, estus={
            "uses": 0, "max": 1, "ash_uses": 0, "ash_max": 1,
            "can_use": False, "can_use_ash": False})))
        # 旧版桥还没给 estus 字段时不该凭空造一行
        self.assertNotIn("元素瓶", view.render(battle_state()))

    def test_view_battle_lists_potion_names_and_block_reasons(self):
        text = view.render(battle_state(potions=[
            {"slot": 0, "id": "ember", "name": "余烬瓶", "desc": "对一名敌人造成 20 伤害", "blocked": ""},
            {"slot": 1, "id": "tonic", "name": "静息药", "desc": "回复光焰",
             "blocked": "只能在大地图/商店里用"}]))
        self.assertIn("[0] 余烬瓶: 对一名敌人造成 20 伤害", text)
        self.assertNotIn("[0] 余烬瓶: 对一名敌人造成 20 伤害（现在用不了", text)
        self.assertIn("[1] 静息药: 回复光焰（现在用不了：只能在大地图/商店里用）", text)
        self.assertNotIn("可用道具", text)  # 旧的裸 [slot]id 行已经废了

    def test_view_campfire_marks_leave_entry_and_action_shape(self):
        text = view.render(campfire_state())
        self.assertIn("[2] 继续前行（离开篝火）", text)
        self.assertIn('{"action":"campfire","slot":序号}', text)
        self.assertIn("[0] 休息：回复 30% 生命", text)
        self.assertNotIn("[0] 休息：回复 30% 生命（离开篝火）", text)

    def test_view_soul_forge_replaces_generic_overlay_body(self):
        text = view.render(soul_forge_state())
        self.assertIn("灵魂锻造，持有 150 灵魂", text)
        self.assertIn("window_id=forge1 revision=f1", text)
        self.assertIn("[0] 横斩 费1 [攻击] 已锻0次", text)
        self.assertIn("[1] 铁壁 费2 [技能] 已锻1次", text)
        self.assertIn("已选卡: [0] 横斩", text)
        self.assertIn("强化 [0] 强化伤害 价 80 ←已选", text)
        self.assertIn("强化 [1] 附加格挡 价 300（灵魂不够）", text)
        for shape in ('{"action":"forge","op":"card","slot":卡牌序号}',
                      '{"action":"forge","op":"option","slot":强化项序号}',
                      '{"action":"forge","op":"forge"}',
                      '{"action":"forge","op":"back"}'):
            self.assertIn(shape, text)
        self.assertNotIn("挡住了地图", text)

    def test_view_soul_forge_free_slot_and_not_forgeable(self):
        self.assertIn("本次免费", view.render(soul_forge_state(free=True)))
        locked = view.render(soul_forge_state(selected={}, options=[], selected_option=-1, can_forge=False))
        self.assertIn("还不能确认锻造", locked)
        self.assertIn("还没选卡", locked)
        self.assertNotIn("已选卡: [", locked)

    def test_view_renders_death_analysis(self):
        text = view.render(panel_state("death_analysis"))
        self.assertIn("死亡分析", text)
        self.assertIn("窗口正文", text)

    def test_view_run_end_is_settlement_text_without_actions(self):
        text = view.render(run_end_state())
        self.assertIn("【当前界面】本局结算", text)
        self.assertIn("恶魔之塔 · 征服", text)
        self.assertIn("击败 BOSS：熔核巨像", text)
        self.assertIn("这一局已结束，会自动返回大厅开下一局", text)
        # 结算页由代码推进，渲染里不能留任何可点动作，也不套「挡住了地图」的说法
        self.assertNotIn("action=overlay", text)
        self.assertNotIn("挡住了地图", text)
        # 桥没给正文（面板刚消失）时也不该抛，仍然是一个可读标题
        self.assertIn("本局结算", view.render({"screen": "run_end"}))
        self.assertIn("本局结算", view.render(run_end_state(hall_button={}, can_back=False, texts=[])))

    def test_mcp_returns_real_failure(self):
        server = Server()
        server.bridge.overlay = Mock(return_value={"ok": False, "error": "窗口未关闭", "closed": False})
        response = server.handle({"id": 1, "method": "tools/call", "params": {
            "name": "overlay_action", "arguments": {"op": "back", "window_id": "one", "revision": "v1"}}})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("窗口未关闭", response["result"]["content"][0]["text"])

    def test_mcp_rejects_missing_tokens(self):
        server = Server()
        server.bridge.overlay = Mock()
        response = server.handle({"id": 2, "method": "tools/call", "params": {
            "name": "overlay_action", "arguments": {"op": "back"}}})
        self.assertTrue(response["result"]["isError"])
        server.bridge.overlay.assert_not_called()

    def test_mcp_success_keeps_after_state(self):
        server = Server()
        server.bridge.event = Mock(return_value={"ok": True, "closed": True, "state_after": {"screen": "map"}})
        response = server.handle({"id": 3, "method": "tools/call", "params": {
            "name": "event_choose", "arguments": {"slot": 0, "window_id": "one", "revision": "v1"}}})
        self.assertFalse(response["result"]["isError"])
        self.assertIn("state_after", response["result"]["content"][0]["text"])
        server.bridge.event.assert_called_once_with(0, "one", "v1")

    def test_view_contains_body_and_controls(self):
        text = view.render(panel_state())
        for expected in ("window_id=one revision=v1", "窗口正文", "action=overlay op=button", "action=event"):
            self.assertIn(expected, text)
        self.assertIn("不要猜 op=back", text)

    def test_mcp_loot_and_death_body_is_readable(self):
        for screen in ("loot", "death"):
            self.assertIn("窗口正文", view.render(panel_state(screen)))

    def test_view_does_not_prompt_locked_choices(self):
        state = panel_state()
        state["event"]["options"][0]["locked"] = True
        text = view.render(state)
        self.assertIn("不可用", text)
        self.assertNotIn("选择未锁定选项:", text)

    def test_player_dispatch_tokens_and_bad_slot(self):
        bridge = Mock()
        bridge.event.return_value = {"ok": True}
        player = Player({}, bridge, narrate=False)
        self.assertTrue(player._do(panel_state(), {"action": "event", "slot": 0})["ok"])
        bridge.event.assert_called_once_with(0, "one", "v1")
        self.assertFalse(player._do(panel_state(), {"action": "event", "slot": "not-a-number"})["ok"])
        bridge.overlay.return_value = {"ok": True}
        player._do(panel_state(), {"action": "overlay", "slot": 0, "op": "button"})
        bridge.overlay.assert_called_once_with(0, "button", "one", "v1")

    def test_view_boss_result_lists_child_label_rewards(self):
        text = view.render(boss_dialog_state())
        for expected in ("window_id=boss1 revision=r1", "◆ 兑换传说卡牌", "直接返回地图", "action=overlay op=button"):
            self.assertIn(expected, text)
        self.assertIn("不要猜 op=back", text)

    def test_system_prompt_protects_settings_and_boss_choice(self):
        self.assertIn("设置窗口只选「关闭」", SYSTEM)
        self.assertIn("Boss魂与章节奖励需要选择奖励", SYSTEM)

    def test_system_prompt_character_is_self_chosen(self):
        """选角不再等人确认：提示里要写明白是模型自己拿主意，否则它会停在选角页。"""
        self.assertIn('{"action":"character","slot":序号}', SYSTEM)
        self.assertIn("选角色你自己拿主意", SYSTEM)
        self.assertIn("不要停下来等确认", SYSTEM)

    def test_system_prompt_offers_estus_and_forge(self):
        """模型不会自己想到「喝瓶不消耗回合」和「锻造分三步」——提示里必须写明白。"""
        self.assertIn('{"action":"estus"}', SYSTEM)
        self.assertIn("不消耗回合", SYSTEM)
        self.assertIn("灰瓶", SYSTEM)
        for shape in ('{"action":"forge","op":"card","slot":卡牌序号}',
                      '{"action":"forge","op":"option","slot":强化项序号}',
                      '{"action":"forge","op":"forge"}',
                      '{"action":"forge","op":"back"}'):
            self.assertIn(shape, SYSTEM)
        self.assertIn("现在用不了", SYSTEM)  # 道具 blocked 原因：别反复撞同一个 slot

    def test_player_dispatch_estus_forge_and_campfire_leave_slot(self):
        bridge = Mock()
        bridge.use_estus.return_value = {"ok": True, "hp": 26, "estus": [0, 2]}
        bridge.soul_forge.return_value = {"ok": True, "state": {"can_forge": True}}
        bridge.campfire.return_value = {"ok": True}
        player = Player({}, bridge, narrate=False)
        self.assertTrue(player._do(battle_state(), {"action": "estus"})["ok"])
        bridge.use_estus.assert_called_once_with(False)
        self.assertTrue(player._do(battle_state(), {"action": "estus", "ash": True})["ok"])
        bridge.use_estus.assert_called_with(True)
        self.assertTrue(player._do(soul_forge_state(), {"action": "forge", "op": "card", "slot": 1})["ok"])
        bridge.soul_forge.assert_called_once_with("card", 1)
        # 继续前行也是篝火的 slot，动作侧要原样转给 campfire
        self.assertTrue(player._do(campfire_state(), {"action": "campfire", "slot": 2})["ok"])
        bridge.campfire.assert_called_once_with("", 2)
        # 界面白名单：越界动作照样被拒，不能拿锻造动作去撞战斗界面
        self.assertFalse(player._do(soul_forge_state(), {"action": "estus"})["ok"])
        self.assertFalse(player._do(battle_state(), {"action": "forge", "op": "back"})["ok"])
        self.assertFalse(player._do(soul_forge_state(), {"action": "forge", "slot": "x"})["ok"])

    def test_observer_rejects_metadata_only(self):
        self.assertFalse(node_regression.readable({"window_id": "one", "revision": "v1", "options": []}))
        self.assertTrue(node_regression.readable({"texts": ["正文"]}))
        self.assertTrue(node_regression.readable({"offers": [{"slot": 0}]}))
        self.assertTrue(node_regression.readable({"tiles": [{"slot": 0}]}))

    def test_observer_no_bridge_is_not_pass(self):
        with patch.object(sys, "argv", ["node_regression.py", "1"]), \
             patch.object(node_regression.time, "monotonic", side_effect=[0, 0, 2]), \
             patch.object(node_regression.time, "sleep"), \
             patch.object(node_regression, "call", side_effect=OSError("offline")), \
             patch("builtins.print"):
            self.assertEqual(node_regression.main(), 2)

    def test_observer_sends_only_reads(self):
        state = panel_state()
        with patch.object(sys, "argv", ["node_regression.py", "1"]), \
             patch.object(node_regression.time, "monotonic", side_effect=[0, 0, 0, 2]), \
             patch.object(node_regression.time, "sleep"), \
             patch.object(node_regression, "call", return_value=state) as call, \
             patch("builtins.print"):
            self.assertEqual(node_regression.main(), 0)
            call.assert_called_once_with("state")


class PlayerTests(unittest.IsolatedAsyncioTestCase):
    def make_player(self):
        bridge = Mock()
        bridge.state.return_value = panel_state()
        bridge.screen.return_value = {"screen": "event", "window_id": "one"}
        bridge.event.return_value = {"ok": True, "closed": True, "screen_after": "map"}
        speak = Mock()
        player = Player({}, bridge, speak=speak)
        player._ask = AsyncMock(return_value={"action": "event", "slot": 0, "say": "领到了"})
        return player, bridge, speak

    async def test_same_screen_new_window_discards_action(self):
        player, bridge, speak = self.make_player()
        bridge.screen.return_value["window_id"] = "two"
        result = await player.step()
        self.assertTrue(result["stale"])
        bridge.event.assert_not_called()
        speak.assert_not_called()

    async def test_bridge_stale_does_not_count_failure(self):
        player, bridge, speak = self.make_player()
        bridge.event.return_value = {"ok": False, "stale": True}
        result = await player.step()
        self.assertTrue(result["stale"])
        self.assertEqual(player.fails, 0)
        speak.assert_not_called()

    async def test_failure_feedback_and_no_false_narration(self):
        player, bridge, speak = self.make_player()
        bridge.event.return_value = {"ok": False, "error": "条件不足"}
        result = await player.step()
        self.assertFalse(result["ok"])
        self.assertIn("条件不足", player.history[-1]["content"])
        speak.assert_not_called()

    async def test_success_reports_closure(self):
        player, bridge, speak = self.make_player()
        result = await player.step()
        self.assertTrue(result["closed"])
        self.assertEqual(result["screen_after"], "map")
        speak.assert_called_once_with("领到了")

    async def test_busy_window_does_not_ask_or_click(self):
        player, bridge, speak = self.make_player()
        bridge.state.return_value["busy"] = True
        result = await player.step()
        self.assertEqual(result["action"]["action"], "wait")
        player._ask.assert_not_called()
        bridge.event.assert_not_called()
        speak.assert_not_called()

    async def test_death_analysis_confirms_without_asking_model(self):
        bridge = Mock()
        bridge.state.return_value = {"screen": "death_analysis", "death_analysis": {"panel": "死亡分析", "texts": ["本轮伤害 60"], "buttons": [{"slot": 0, "text": "确认死亡"}]}}
        bridge.confirm_death_analysis = Mock(return_value={"ok": True, "closed": True, "screen_after": "map"})
        player = Player({}, bridge, narrate=False)
        player._ask = AsyncMock()
        result = await player.step()
        self.assertTrue(result["ok"])
        bridge.confirm_death_analysis.assert_called_once()
        player._ask.assert_not_awaited()

    async def test_death_analysis_failure_reported(self):
        bridge = Mock()
        bridge.state.return_value = {"screen": "death_analysis", "death_analysis": {"panel": "死亡分析", "texts": [], "buttons": []}}
        bridge.confirm_death_analysis = Mock(return_value={"ok": False, "error": "找不到确认死亡按钮"})
        player = Player({}, bridge, narrate=False)
        player._ask = AsyncMock()
        result = await player.step()
        self.assertFalse(result["ok"])
        self.assertIn("确认死亡", result["error"])
        player._ask.assert_not_awaited()

    def make_run_end_player(self, reply):
        bridge = Mock()
        bridge.state.return_value = run_end_state()
        bridge.screen.return_value = {"screen": "run_end", "window_id": "win1"}
        bridge.return_to_hall = Mock(return_value=reply)
        player = Player({}, bridge, narrate=False)
        player._ask = AsyncMock()
        return player, bridge

    async def test_run_end_returns_to_hall_without_asking_model(self):
        player, bridge = self.make_run_end_player(
            {"ok": True, "closed": True, "screen_after": "hall", "error": ""})
        result = await player.step()
        self.assertEqual(result["screen"], "run_end")
        self.assertEqual(result["action"], {"action": "return_to_hall"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["error"], "")
        self.assertEqual(result["say"], "")
        bridge.return_to_hall.assert_called_once()
        player._ask.assert_not_awaited()
        # 「返回大厅」不在 buttons 里，任何 overlay 动作都不该被发到结算页
        bridge.overlay.assert_not_called()

    async def test_run_end_failure_reported_without_asking_model(self):
        player, bridge = self.make_run_end_player(
            {"ok": False, "closed": False, "screen_after": "run_end",
             "error": "点了返回大厅但没回到出征大厅（现在在 run_end）"})
        result = await player.step()
        self.assertFalse(result["ok"])
        self.assertIn("没回到出征大厅", result["error"])
        self.assertEqual(result["action"], {"action": "return_to_hall"})
        player._ask.assert_not_awaited()

    async def test_step_drinks_estus_in_battle(self):
        bridge = Mock()
        bridge.state.return_value = battle_state(hp=10, max_hp=40, estus={
            "uses": 1, "max": 2, "ash_uses": 0, "ash_max": 0, "can_use": True, "can_use_ash": False})
        bridge.screen.return_value = {"screen": "battle"}
        bridge.use_estus.return_value = {"ok": True, "error": "", "hp": 26, "estus": [0, 2]}
        player = Player({}, bridge, narrate=False)
        player._ask = AsyncMock(return_value={"action": "estus", "say": "先喝一口"})
        result = await player.step()
        self.assertTrue(result["ok"])
        self.assertNotIn("stale", result)
        bridge.use_estus.assert_called_once_with(False)

    async def test_step_forge_keeps_window_and_slot(self):
        bridge = Mock()
        bridge.state.return_value = soul_forge_state()
        bridge.screen.return_value = {"screen": "soul_forge", "window_id": "forge1"}
        bridge.soul_forge.return_value = {"ok": True, "error": "", "state": {"can_forge": True}}
        player = Player({}, bridge, narrate=False)
        player._ask = AsyncMock(return_value={"action": "forge", "op": "option", "slot": 0, "say": "强化伤害"})
        result = await player.step()
        self.assertTrue(result["ok"])
        bridge.soul_forge.assert_called_once_with("option", 0)

    async def test_step_drops_stale_forge_when_window_changed(self):
        bridge = Mock()
        bridge.state.return_value = soul_forge_state()
        bridge.screen.return_value = {"screen": "soul_forge", "window_id": "forge2"}
        player = Player({}, bridge, narrate=False)
        player._ask = AsyncMock(return_value={"action": "forge", "op": "forge", "say": ""})
        result = await player.step()
        self.assertTrue(result["stale"])
        bridge.soul_forge.assert_not_called()

    def make_dialog_player(self, state):
        bridge = Mock()
        bridge.state.return_value = state
        bridge.screen.return_value = {"screen": state["screen"], "window_id": state.get("window_id")}
        bridge.overlay.return_value = {"ok": True, "closed": True, "screen_after": "map"}
        return Player({}, bridge, narrate=False), bridge

    async def test_dialog_boss_reward_routes_overlay_with_tokens(self):
        player, bridge = self.make_dialog_player(boss_dialog_state())
        player._ask = AsyncMock(return_value={"action": "overlay", "slot": 1, "op": "button", "say": "拿了传说卡"})
        result = await player.step()
        self.assertTrue(result["ok"])
        self.assertTrue(result["closed"])
        bridge.overlay.assert_called_once_with(1, "button", "boss1", "r1")
        bridge.call.assert_not_called()

    async def test_dialog_settings_back_forwards_tokens(self):
        state = boss_dialog_state("set1", "s1", panel="SettingsPanel")
        state["dialog"]["can_back"] = True
        state["dialog"]["buttons"] = [{"slot": 0, "text": "关闭"}]
        player, bridge = self.make_dialog_player(state)
        player._ask = AsyncMock(return_value={"action": "overlay", "op": "back", "say": "关掉设置"})
        result = await player.step()
        self.assertTrue(result["ok"])
        bridge.overlay.assert_called_once_with(-1, "back", "set1", "s1")
        bridge.call.assert_not_called()

    async def test_dialog_window_change_drops_stale_reward(self):
        player, bridge = self.make_dialog_player(boss_dialog_state())
        player._ask = AsyncMock(return_value={"action": "overlay", "slot": 1, "op": "button", "say": ""})
        bridge.screen.return_value = {"screen": "dialog", "window_id": "two"}
        result = await player.step()
        self.assertTrue(result["stale"])
        bridge.overlay.assert_not_called()
        bridge.call.assert_not_called()


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_bridge_does_not_block_web_requests(self):
        import threading
        from aiohttp.test_utils import TestClient, TestServer
        from main import build_web
        app = Mock()
        app.cfg = {"dtgame": {}}
        app.dt = None
        app.game.running.return_value = False
        app._config_problem.return_value = ""
        started, release = threading.Event(), threading.Event()
        def slow_screen():
            started.set()
            release.wait(2)
            return {"screen": "map"}
        app._dt_bridge.return_value.screen.side_effect = slow_screen
        client = TestClient(TestServer(build_web(app)))
        await client.start_server()
        status = asyncio.create_task(client.get("/api/dtgame/status"))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            response = await client.get("/")
            self.assertEqual(response.status, 200)
            self.assertFalse(status.done())
            release.set()
            response = await status
            self.assertTrue((await response.json())["ready"])
        finally:
            release.set()
            await status
            await client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
