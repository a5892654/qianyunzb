"""从已读到的局势识别反应时机，控制解说长短和频率。"""
import collections
import json
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
        mode, label = "tense", "战况紧张，用简短有反应的一句话解说，保持和观众交流"
    elif screen == "battle" and not settled:
        mode, label = "battle", "正在打牌，自然解说当下，优先回应观众"
    elif screen in ("reward", "campfire", "shop", "map", "event", "loot", "hall", "run_end") or settled:
        mode, label = "rest", "正在选择或结算，可以顺着聊天自然多说一两句"
    else:
        mode, label = "neutral", "自然闲聊，句子有长有短，不必每次追问"
    return {"mode": mode, "label": label, "max_chars": 18 if mode == "tense" else 26 if mode == "battle" else 40}


LINES = {
    "low_health": ("别慌，我先认真看看。", "血线有点悬，先不贪了。", "嘴硬归嘴硬，这会儿得稳住。"),
    "hurt": ("嘶，这下真疼，记住了。", "这波吃亏了，我得留神。", "刚才那一下，疼得很具体。"),
    "heal": ("这口补上来，心里踏实了。", "缓过来一点，接着打。", "呼，血条总算有点精神了。"),
    "boss": ("头目来了，我先看清楚。", "这场不闹了，认真拆招。", "好，硬仗来了，咱们稳住。"),
    "win": ("拿下！我还是有两下子的嘛。", "过了过了，允许我得意一下。", "这场打完了，先松口气。"),
    "clutch_win": ("呼，刚才真悬，给我紧张的。", "险胜也是胜，先让我缓口气。", "撑过来了，我的心终于坐下了。"),
    "death": ("行，这波是我没打好，认了。", "嘴硬没救回来，下把得改。", "倒了，先看看哪儿出了问题。"),
    "relic": ("新遗物到手，看看能玩什么花样。", "收下了，我有点期待这个配合。", "欸，多了件宝贝，我研究一下。"),
}
PRIORITY = {"death": 100, "clutch_win": 95, "win": 90, "low_health": 80,
            "hurt": 60, "relic": 50, "boss": 40, "heal": 30}


class GameCommentary:
    MOMENT_TTL = 20

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.previous = None
        self.battle_key = None
        self.lowest_ratio = 1.0
        self.pending = None
        self.seen = collections.deque(maxlen=128)
        self.counts = collections.Counter()
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
                  + f"。本次 say 请说一句约{min(situation['max_chars'], 30)}字的自然口语，保持边玩边聊，"
                  "也不要提前宣告尚未发生的胜利、回血或奖励。")
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

    def filler(self, state):
        """模型漏写或跑题时，用一句贴合局面的短解说补上，不再多等一轮。"""
        lines = (("先别慌，我认真看看。", "这会儿先顾好眼前。", "这里得稳住，不能贪。", "我看清楚再动。")
                 if scene(state)["mode"] == "tense" else
                 ("接着打，我还盯着呢。", "我看看这手怎么接。", "这步得动点小脑筋。", "先把眼前这步走好。", "这儿可得看仔细。")
                 if scene(state)["mode"] == "battle" else
                 ("好，咱们接着来。", "我看看有什么花样。", "这里让我琢磨一下。", "我还挺好奇后面会怎样。", "不急，我先看一眼。"))
        # 优先选最近没说过的，避免模型持续漏写时只在两句话之间来回切换。
        recent = list(self.recent)
        return min(lines, key=lambda line: max((i + 1 for i, old in enumerate(recent) if old == line), default=0))

    def can_speak(self, state, text, important=False):
        return bool(text) and (not self.recent or text != self.recent[-1])

    def spoken(self, state, text):
        moment = self.moment()
        if moment:
            self.counts[moment["kind"]] += 1
        self.pending = None
        self.recent.append(text)
