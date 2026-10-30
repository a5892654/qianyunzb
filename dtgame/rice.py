"""从 AI 自己的操作和现有只读游戏状态识别第三幕 Boss 胜利。"""
import hashlib

ACT3_BOSSES = {
    "storm_overlord_boss", "abyss_heart_remnant", "fallen_knight_boss", "abyss_devourer_kulo",
    "风暴之王·迦楼罗", "深渊之心·阿比斯残影", "堕落冒险者·灰烬骑士", "深渊吞星者·库洛",
}
COMBAT_ACTIONS = {"play", "end", "potion", "radiance", "estus", "overlay"}


class RiceTracker:
    def __init__(self, counter, enabled, on_award=lambda: None):
        self.counter, self.enabled, self.on_award = counter, enabled, on_award
        self.pause()

    def pause(self):
        self.pending = None
        self.ai_acted = False

    def _identity(self, state):
        run = state.get("run") or {}
        stable = next((run[k] for k in ("run_id", "id", "run_seed", "seed") if run.get(k) not in (None, "")), None)
        return str(stable) if stable is not None else self.counter.session

    @staticmethod
    def _act(state):
        return (state.get("run") or {}).get("act")

    @staticmethod
    def _boss(state):
        battle = state.get("battle") or {}
        if battle.get("type", battle.get("battle_type")) == "boss":
            return True
        return any(e.get("type") == "boss" or e.get("id") in ACT3_BOSSES or
                   any(name in str(e.get("name", "")) for name in ACT3_BOSSES if "·" in name)
                   for e in battle.get("enemies") or [] if isinstance(e, dict))

    def action(self, state, action, result):
        if not result.get("ok"):
            return
        if action.get("action") == "expedition":
            self.counter.begin_run()
            self.pause()
            return
        if not self.enabled():
            self.pause()
            return
        if self._act(state) != 3:
            return
        screen = state.get("screen")
        if screen == "map" and action.get("action") == "node":
            choices = (state.get("map") or {}).get("choices") or []
            chosen = next((c for c in choices if c.get("col") == action.get("col") and c.get("row") == action.get("row")), {})
            if chosen.get("type") == "boss" and not chosen.get("passthrough"):
                self.pending, self.ai_acted = self._identity(state), False
        elif screen == "battle" and action.get("action") in COMBAT_ACTIONS:
            if self._boss(state) or self.pending == self._identity(state):
                self.pending, self.ai_acted = self._identity(state), True

    def observe(self, state):
        if not self.enabled():
            self.pause()
            return
        if not self.pending or not self.ai_acted:
            return
        if self.pending != self._identity(state):
            self.pause()
            return
        screen = state.get("screen")
        run = state.get("run") or {}
        battle = state.get("battle") or {}
        hp = battle.get("hp", run.get("hp"))
        if screen in ("death", "death_analysis", "hall", "menu", "character_select") or (isinstance(hp, (int, float)) and hp <= 0):
            self.pause()
            return
        if hp is None or hp <= 0:
            return
        body = state.get(screen) or {}
        title = " ".join(str(t) for t in [body.get("title", ""), body.get("panel", ""), *(body.get("texts") or [])])
        won = (battle.get("victory") is True or battle.get("awaiting_confirm") is True or
               (screen == "battle" and battle.get("battle_over") is True and
                bool(battle.get("enemies")) and all(e.get("hp", 1) <= 0 for e in battle["enemies"])) or
               screen in ("loot", "reward") or
               (screen == "dialog" and ("Boss魂" in title or "DepthChoiceOverlay" in title)) or
               self._act(state) == 4)
        if won and self._act(state) in (3, 4):
            key = hashlib.sha256(("act3:" + self.pending).encode()).hexdigest()
            if self.counter.award(key):
                self.on_award()
            self.pause()
