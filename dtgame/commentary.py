"""从已读到的局势识别反应时机，控制解说长短和频率。"""
import collections
import json
import random
import time

from dtgame.rice import RiceTracker


def number(value):
    return value if type(value) in (int, float) else None


def scene(state):
    state = state or {}
    screen, battle, run = state.get("screen"), state.get("battle") or {}, state.get("run") or {}
    hp, maximum = number(battle.get("hp", run.get("hp"))), number(battle.get("max_hp", run.get("max_hp")))
    incoming = sum(max(0, number((e.get("intent") or {}).get("value")) or 0)
                   * max(1, number((e.get("intent") or {}).get("hits")) or 1)
                   for e in battle.get("enemies") or [] if e.get("alive", True)
                   and (e.get("intent") or {}).get("type") == "attack")
    danger = (hp is not None and hp > 0 and
              ((maximum and hp / maximum <= 0.25) or incoming - (number(battle.get("block")) or 0) >= hp))
    settled = battle.get("battle_over") or battle.get("victory") or battle.get("awaiting_confirm")
    if state.get("busy") or (screen == "battle" and not settled and (danger or RiceTracker._boss(state))):
        mode, label = "tense", "局势紧张，先专心操作，回应观众时简短接话，不主动展开闲聊或求关注"
    elif screen == "battle" and not settled:
        mode, label = "battle", "正在打牌，话少一点，优先回应观众，不逐张牌念操作"
    elif screen in ("reward", "campfire", "shop", "map", "event", "loot", "hall", "run_end") or settled:
        mode, label = "rest", "正在选择或结算，可以顺着聊天自然多说一两句"
    else:
        mode, label = "neutral", "自然闲聊，句子有长有短，不必每次追问"
    return {"mode": mode, "label": label, "max_chars": 18 if mode == "tense" else 26 if mode == "battle" else 40}


LINES = {
    "low_health": ("这下有点悬了，我先稳住！", "血线压下来了，得认真一点了。", "哎哟，这会儿可不能贪了。"),
    "hurt": ("哎，这一下掉血有点疼！", "这波吃亏了，后面得更小心。", "嘶，这下真有点肉疼。"),
    "heal": ("缓过来一点了，接着稳稳打。", "血补上来一些，心里踏实多了。", "这口补得舒服，继续慢慢来。"),
    "boss": ("到头目这儿了，我先认真看一下。", "这场得留点神，先看清楚再出手。", "好，硬仗来了，慢慢拆招。"),
    "win": ("拿下了！这会儿能松口气了。", "这场过了，缓一缓再继续！", "收工收工，看看接下来有什么。"),
    "clutch_win": ("呼，撑过去了，刚才真有点悬！", "这也挺过来了！刚才心都提起来了。", "险归险，总算拿下了，喘口气。"),
    "death": ("哎，还是倒了，这波得记一下教训。", "这次没撑住，缓一缓再说。", "这波吃亏了，得好好想想。"),
    "relic": ("新的遗物到手了，看看能怎么搭。", "多了一件遗物，试试新的配合。", "欸，收下这件遗物，看看效果。"),
}
PRIORITY = {"death": 100, "clutch_win": 95, "win": 90, "low_health": 80,
            "hurt": 60, "relic": 50, "boss": 40, "heal": 30}


class GameCommentary:
    MOMENT_TTL = 20

    def __init__(self, clock=time.monotonic, jitter=random.uniform):
        self.clock, self.jitter = clock, jitter
        self.previous = None
        self.battle_key = None
        self.lowest_ratio = 1.0
        self.pending = None
        self.seen = collections.deque(maxlen=128)
        self.counts = collections.Counter()
        self.last_spoken = None
        self.next_speech = 0
        self.recent = collections.deque(maxlen=6)

    def _cue(self, kind, token, facts):
        token = (kind, token)
        if token in self.seen:
            return
        self.seen.append(token)
        cue = {"kind": kind, "facts": facts, "at": self.clock()}
        if not self.moment() or PRIORITY[kind] >= PRIORITY[self.pending["kind"]]:
            self.pending = cue

    def observe(self, state):
        screen, run, battle = state.get("screen"), state.get("run") or {}, state.get("battle") or {}
        hp, maximum = number(battle.get("hp", run.get("hp"))), number(battle.get("max_hp", run.get("max_hp")))
        run_key = tuple(str(run.get(k) or "") for k in ("run_id", "id", "seed", "run_seed", "character"))
        enemies = tuple(str(e.get("id") or e.get("name") or i) for i, e in enumerate(battle.get("enemies") or []))
        key = (run_key, (battle.get("life") or {}).get("battle_id") or battle.get("battle_id")
               or (run.get("act"), run.get("floor"), enemies))
        relics = tuple(str(r) for r in run.get("relics") or [])
        previous = self.previous
        same_run = previous is not None and previous["run"] == run_key
        if not same_run or screen in ("hall", "menu", "character_select"):
            self.pending = None
            self.battle_key = None
            self.lowest_ratio = 1.0
            if not same_run or not previous or previous["screen"] != screen:
                self.seen.clear()
        if screen == "battle":
            if key != self.battle_key:
                self.pending = None
                self.battle_key, self.lowest_ratio = key, 1.0
                if RiceTracker._boss(state) and not battle.get("battle_over"):
                    self._cue("boss", key, "已进入头目战，胜负尚未确定")
            if hp is not None and maximum and hp > 0:
                self.lowest_ratio = min(self.lowest_ratio, hp / maximum)
                if hp / maximum <= 0.25 and not battle.get("battle_over"):
                    self._cue("low_health", key, {"当前生命": hp, "生命上限": maximum})
                if same_run and previous["screen"] == "battle" and previous["battle"] == key and previous["hp"] is not None:
                    change = hp - previous["hp"]
                    if abs(change) >= maximum * 0.2:
                        kind = "heal" if change > 0 else "hurt"
                        self._cue(kind, (key, battle.get("turn"), previous["hp"], hp),
                                  {"生命变化": change, "当前生命": hp})
            enemies_dead = bool(battle.get("enemies")) and all(
                number(e.get("hp")) is not None and e["hp"] <= 0 for e in battle["enemies"])
            if hp is not None and hp > 0 and (battle.get("victory") is True or (battle.get("battle_over") and enemies_dead)):
                self._win(key)
        elif screen in ("loot", "reward") and same_run and previous["screen"] == "battle" and hp is not None and hp > 0:
            self._win(previous["battle"])
        elif screen in ("death", "death_analysis") and (not same_run or previous["screen"] not in ("death", "death_analysis")):
            self.pending = None
            self._cue("death", self.battle_key or key, "游戏已显示死亡结算；不推测复活是否成功")
        if (same_run and previous["screen"] not in ("hall", "menu", "character_select")
                and screen not in ("hall", "menu", "character_select")):
            gained = set(relics) - set(previous["relics"])
            if gained:
                self._cue("relic", (run_key, relics), {"新获得遗物": sorted(gained)})
        self.previous = {"screen": screen, "run": run_key, "battle": key, "hp": hp, "relics": relics}

    def _win(self, key):
        kind = "clutch_win" if self.lowest_ratio <= 0.25 else "win"
        self._cue(kind, key, "已经确认战斗获胜" + ("，本场曾处于低血线" if kind == "clutch_win" else ""))

    def moment(self):
        if self.pending and self.clock() - self.pending["at"] >= self.MOMENT_TTL:
            self.pending = None
        return self.pending

    def prompt(self, state):
        situation = scene(state)
        prompt = ("\n【直播说话节奏】" + situation["label"]
                  + f"。say 可留空；有值得说的才说，约{min(situation['max_chars'], 30)}字以内，"
                  "不用每次都说明下一步操作，也不要提前宣告尚未发生的胜利、回血或奖励。")
        moment = self.moment()
        if moment:
            prompt += ("\n【刚发生的真实变化】" + json.dumps(moment["facts"], ensure_ascii=False)
                       + "。这次优先自然反应这件事，先有感受再考虑一句想法，不要复述数值或编造原因。")
        return prompt

    def fallback(self):
        moment = self.moment()
        if not moment:
            return ""
        kind = moment["kind"]
        return LINES[kind][self.counts[kind] % len(LINES[kind])]

    def can_speak(self, state, text, important=False):
        if not text or text in self.recent:
            return False
        return important or self.last_spoken is None or self.clock() >= self.next_speech

    def spoken(self, state, text):
        moment = self.moment()
        if moment:
            self.counts[moment["kind"]] += 1
        self.pending = None
        self.recent.append(text)
        self.last_spoken = self.clock()
        gap = {"tense": 20, "battle": 10, "rest": 7, "neutral": 10}[scene(state)["mode"]]
        self.next_speech = self.last_spoken + gap + self.jitter(0, gap * 0.5)
