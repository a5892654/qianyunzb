import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dtgame import forge, view


def forge_state(**over):
    body = {
        "window_id": "forge1", "revision": "r1", "souls": 200, "free": False,
        "cards": [{"slot": 0, "id": "heavy", "name": "重斩", "type": "attack", "cost": 2,
                   "damage": 10, "block": 0, "desc": "造成 10 点伤害。",
                   "forge_count": 0, "count": 1}],
        "selected": {"slot": 0, "id": "heavy", "name": "重斩", "type": "attack", "cost": 2,
                     "damage": 10, "block": 0, "desc": "造成 10 点伤害。", "forge_count": 0},
        "options": [
            {"slot": 0, "name": "淬炼锋刃", "type": "strengthen", "cost": 50,
             "preview": "伤害 10 → 13", "affordable": True},
            {"slot": 1, "name": "轻灵化（限一次）", "type": "sharpen", "cost": 80,
             "preview": "费用 2 → 1", "affordable": True},
        ],
        "selected_option": 0, "can_forge": True,
    }
    body.update(over)
    return {"screen": "soul_forge", "window_id": "forge1", "soul_forge": body}


class ForgeTests(unittest.TestCase):
    def test_cost_reduction_beats_flat_damage_on_expensive_card(self):
        slot, reason = forge.recommend(forge_state()["soul_forge"])
        self.assertEqual(slot, 1)
        self.assertIn("降 1 费", reason)
        text = "\n".join(forge.notes(forge_state()["soul_forge"]))
        self.assertIn("优先推荐 [1] 轻灵化（限一次）", text)
        self.assertIn("伤害 10 → 13", view.render(forge_state()))
        self.assertIn("★推荐", view.render(forge_state()))

    def test_unaffordable_options_are_not_recommended(self):
        state = forge_state()
        state["soul_forge"]["souls"] = 60
        state["soul_forge"]["options"][1]["affordable"] = False
        slot, _ = forge.recommend(state["soul_forge"])
        self.assertEqual(slot, 0)

    def test_replay_enchant_ranks_above_flat_damage(self):
        state = forge_state()
        state["soul_forge"]["selected"]["cost"] = 1
        state["soul_forge"]["options"][1]["preview"] = "费用 1 → 0"
        state["soul_forge"]["options"].append(
            {"slot": 2, "name": "附魔·重奏", "type": "enchant_replay", "cost": 120,
             "preview": "效果触发两次", "affordable": True})
        slot, _ = forge.recommend(state["soul_forge"])
        self.assertEqual(slot, 2)

    def test_no_selected_card_asks_for_one(self):
        state = forge_state(selected={}, options=[], selected_option=-1, can_forge=False)
        text = "\n".join(forge.notes(state["soul_forge"]))
        self.assertIn("先选一张卡", text)

    def test_repeated_forge_flags_scaling_cost(self):
        state = forge_state()
        state["soul_forge"]["selected"]["forge_count"] = 2
        text = "\n".join(forge.notes(state["soul_forge"]))
        self.assertIn("费用 ×2.5", text)
        self.assertIn("换一张更划算", text)

    def test_free_mode_still_recommends_without_price(self):
        state = forge_state(free=True)
        for option in state["soul_forge"]["options"]:
            option["affordable"] = True
        slot, _ = forge.recommend(state["soul_forge"])
        self.assertIsNotNone(slot)
        self.assertIn("免费", "\n".join(forge.notes(state["soul_forge"])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
