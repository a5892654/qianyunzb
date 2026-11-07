import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dtgame import tactics, view


def battle(**over):
    b = {
        "turn": 2, "energy": 3, "max_energy": 3,
        "hp": 30, "max_hp": 60, "block": 0,
        "strength": 0, "dexterity": 0, "block_mult": 1.0, "fragile": False,
        "enemies": [{"name": "灰烬骑士", "hp": 30, "max_hp": 30, "block": 0,
                     "alive": True, "targetable": True,
                     "intent": {"type": "attack", "name": "重劈", "value": 12, "hits": 1}}],
        "hand": [{"slot": 0, "name": "横斩", "cost": 1, "type": "attack", "target": "single",
                  "desc": "造成 8 点伤害", "damage": 8, "hits": 1, "block": 0,
                  "playable": True, "blocked": ""}],
        "estus": {"uses": 2, "max": 2, "can_use": True},
    }
    b.update(over)
    return b


class TacticsTests(unittest.TestCase):
    def test_multi_hit_and_block_estimate(self):
        b = battle(
            hp=5,
            enemies=[{"name": "双刀客", "hp": 20, "block": 0, "alive": True,
                      "intent": {"type": "attack", "name": "连斩", "value": 4, "hits": 3}}],
            hand=[{"slot": 0, "name": "铁壁", "cost": 1, "type": "skill",
                   "block": 5, "damage": 0, "playable": True, "blocked": ""}])
        text = "\n".join(tactics.notes(b))
        self.assertIn("总伤害 12（双刀客 12（4×3））", text)
        self.assertIn("最多约 5 点格挡", text)
        self.assertIn("即使全防也会倒下", text)
        self.assertIn("元素瓶现在能喝", text)

    def test_lethal_window_skips_wasteful_block(self):
        b = battle(
            energy=2,
            enemies=[{"name": "残血怪", "hp": 8, "block": 0, "alive": True,
                      "intent": {"type": "attack", "name": "抓", "value": 30, "hits": 1}}],
            hand=[{"slot": 0, "name": "重斩", "cost": 2, "type": "attack",
                   "damage": 9, "hits": 1, "block": 0, "playable": True, "blocked": ""}])
        text = "\n".join(tactics.notes(b))
        self.assertIn("有斩杀窗口", text)
        self.assertNotIn("先补格挡", text)

    def test_energy_budget_picks_best_damage(self):
        b = battle(
            energy=2,
            enemies=[{"name": "木桩", "hp": 100, "block": 0, "alive": True,
                      "intent": {"type": "defend", "name": "防守"}}],
            hand=[{"slot": 0, "name": "小刀", "cost": 1, "damage": 5, "hits": 1,
                   "block": 0, "playable": True, "blocked": ""},
                  {"slot": 1, "name": "重锤", "cost": 2, "damage": 12, "hits": 1,
                   "block": 0, "playable": True, "blocked": ""},
                  {"slot": 2, "name": "打不出的神卡", "cost": 3, "damage": 99, "hits": 1,
                   "block": 0, "playable": False, "blocked": "能量不足（需 3，有 2）"}])
        text = "\n".join(tactics.notes(b))
        self.assertIn("12 点伤害", text)
        self.assertNotIn("99", text)
        self.assertIn("敌人本回合没有攻击意图", text)

    def test_old_bridge_without_card_numbers_does_not_invent_math(self):
        b = battle(hand=[{"slot": 0, "name": "横斩", "cost": 1, "type": "attack",
                          "target": "single", "desc": "造成 8 点伤害", "blocked": ""}])
        text = "\n".join(tactics.notes(b))
        self.assertIn("敌方本回合总伤害 12", text)
        self.assertIn("牌面数值缺失", text)
        self.assertNotIn("点伤害，", text)

    def test_rendered_battle_includes_numbers_and_advisor(self):
        text = view.render({
            "screen": "battle", "run": {"character": "灰烬", "hp": 30, "max_hp": 60,
                                        "souls": 10, "deck": ["横斩", "横斩", "铁壁"]},
            "battle": battle(
                hand=[{"slot": 0, "name": "横斩", "cost": 1, "type": "attack",
                       "target": "single", "desc": "造成 8 点伤害",
                       "damage": 8, "hits": 2, "block": 0, "playable": True, "blocked": ""},
                      {"slot": 1, "name": "铁壁", "cost": 1, "type": "skill",
                       "target": "self", "desc": "获得 5 点格挡",
                       "damage": 0, "block": 5, "playable": True, "blocked": ""}]),
        })
        self.assertIn("[0] 横斩 费1 [攻击] 需选敌 · 伤害8×2", text)
        self.assertIn("[1] 铁壁 费1 [技能] 自身 · 格挡5", text)
        self.assertIn("牌组 3 张: 横斩×2、铁壁", text)
        self.assertIn("【作战参谋】", text)
        self.assertIn("敌方本回合总伤害 12", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
