"""上班摸鱼直播：工资/专注/白干/被抓状态机与主播 AI 台词。

节目形式参考「我才不是大肥鱼--」公开直播切片：
- 观众发「摸鱼」让主播开小差，发「老板来了」抓包；
- 礼物把手速和工资顶上去；
- 工资到 6,000 就下班，第二天重新打卡。
画面由 live_wage.WageRenderer 渲染，台词只播模型生成的句子。
"""
from __future__ import annotations

import collections
import copy
import json
import logging
import math
import os
import random
import threading
import time
from datetime import datetime

import styles
from wage_play import OFFICE_DEFAULTS, OfficePlay, career_defaults, clean_career
from wage_slack import SLACK_BY_ID, SLACK_COMMANDS, INTERACTIVE, SlackArcade, book_defaults, clean_book, event_view

log = logging.getLogger("wage")

DEFAULTS = {
    "enabled": True,
    "target": 6000,
    "base_minutes": 60,
    "work_reward": 100,
    "narrate": True,
    "idle_line_seconds": 25,
    "speech_hold": 6.0,
    "slack_seconds": 10.0,
    "catch_seconds": 5.0,
    "slack_mean_seconds": 120,
    "catch_rate": 0.06,
    "slack_penalty": 0,
    "catch_penalty": 500,
    "speed_per_yuan": 0.1,
    "speed_decay_per_second": 0.005,
    "max_speed": 9.9,
    "next_day_seconds": 45,
    "auto_slack": True,
    "poll_enabled": True,
    "poll_seconds": 30,
    "poll_cooldown": 90,
    "persona": "",
    "avatar_x": 0.52,
    "avatar_y": 0.67,
    "avatar_size": 0.78,
    "avatar_zoom": 1.8,
    "boss_scale": 1.0,
    "typing_enabled": True,
    "boss_sfx_enabled": True,
    "boss_sfx_volume": 45,
    "boss_sfx_cooldown": 3.0,
}
DEFAULTS.update(OFFICE_DEFAULTS)

_WEEKDAYS = "一二三四五六日"
_SLACK_WORDS = ("摸鱼", "偷懒", "划水", "开小差", "歇会", "休息一下", "别卷了")
_CATCH_WORDS = ("老板来了", "老板来", "查岗", "抓包", "抓你", "举报", "主管来了")
_CLOCK_WORDS = ("上班", "打卡")
_OFF_WORDS = ("下班", "收工")


def avatar_layout(settings, state):
    """大鲨总出场时把主播留在中间，避免手臂挡住脸；不改用户保存的位置。"""
    x = float(settings.get('avatar_x', DEFAULTS['avatar_x']))
    boss = (state.get('status') == 'caught' or state.get('poll')
            or (state.get('office') or {}).get('boss_pose'))
    if boss:
        x = min(x, .56)
    return ('br', float(settings.get('avatar_size', DEFAULTS['avatar_size'])), 0.0,
            x, float(settings.get('avatar_y', DEFAULTS['avatar_y'])))


def _number(value, default, low=None, high=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    if low is not None and value < low:
        return low
    if high is not None and value > high:
        return high
    return value


def validate_settings(patch):
    if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
        raise ValueError("上班摸鱼设置无效")
    checks = {
        "target": (100, 1_000_000), "base_minutes": (1, 240), "work_reward": (1, 10000),
        "idle_line_seconds": (20, 3600),
        "speech_hold": (0, 60), "slack_seconds": (3, 60), "catch_seconds": (1, 30),
        "slack_mean_seconds": (30, 7200), "catch_rate": (0, 1), "slack_penalty": (0, 100000),
        "catch_penalty": (0, 100000), "speed_per_yuan": (0, 10), "speed_decay_per_second": (0, 1),
        "max_speed": (1, 99.9), "next_day_seconds": (5, 3600),
        "poll_seconds": (10, 120), "poll_cooldown": (30, 3600),
        "avatar_x": (0, 1), "avatar_y": (0, 1), "avatar_size": (0.2, 1.6),
        "avatar_zoom": (1, 3),
        "boss_scale": (.6, 1.5),
        "boss_sfx_volume": (0, 100), "boss_sfx_cooldown": (2, 15),
        "rescue_seconds": (3, 30), "task_interval_seconds": (30, 3600),
        "task_seconds": (10, 180), "rule_seconds": (10, 120),
    }
    for key, (low, high) in checks.items():
        if key in patch:
            if type(patch[key]) not in (int, float) or not math.isfinite(patch[key]) \
                    or not low <= patch[key] <= high:
                raise ValueError(f"上班摸鱼参数 {key} 超出范围")
    if "enabled" in patch and type(patch["enabled"]) is not bool:
        raise ValueError("上班摸鱼开关无效")
    if "narrate" in patch and type(patch["narrate"]) is not bool:
        raise ValueError("上班摸鱼解说开关无效")
    if "auto_slack" in patch and type(patch["auto_slack"]) is not bool:
        raise ValueError("自动摸鱼开关无效")
    if "poll_enabled" in patch and type(patch["poll_enabled"]) is not bool:
        raise ValueError("紧急投票开关无效")
    for key in ('rescue_enabled', 'tasks_enabled', 'items_enabled', 'rules_enabled', 'career_enabled', 'typing_enabled', 'boss_sfx_enabled'):
        if key in patch and type(patch[key]) is not bool:
            raise ValueError(f'办公室玩法开关 {key} 无效')
    if "persona" in patch and (not isinstance(patch["persona"], str) or len(patch["persona"]) > 2000):
        raise ValueError("上班摸鱼人设过长")


class WageEngine:
    """线程安全状态机；渲染线程读，事件线程和 AI 线程写。"""

    def __init__(self, settings, room_id=0, persist_path=None):
        self._settings = settings if callable(settings) else (lambda: settings)
        self.room_id = str(room_id or "")
        self.persist_path = os.fspath(persist_path) if persist_path else None
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._renderer = None
        self._renderer_room = None
        self._floaters = []
        self._cues = collections.deque(maxlen=12)
        self._effects = collections.deque(maxlen=12)
        self._speech = ""
        self._speech_until = None
        self._viewer = None
        self._poll = None
        self._poll_votes = {}
        self._last_poll = float("-inf")
        self._paused = False
        self._away = False
        self._away_since = 0.0
        self._seen_events = collections.OrderedDict()
        self._last_tick = time.monotonic()
        self._last_frame = 0.0
        self._last_save = 0.0
        self._dirty = False
        self._slack_total = 10.0
        self._caught_total = 2.5
        self._last_cheer = float("-inf")
        self._auto_slack_mean = None
        self._auto_slack_left = 0.0
        self._slack_bag = []
        self._slack_event = None
        self._slack_seq = 0
        self.state = self._load()
        self.play = OfficePlay(self)
        self.arcade = SlackArcade(self)

    # ---------- 配置 / 日期 ----------
    def settings(self):
        return {**DEFAULTS, **dict(self._settings() or {})}

    def _today(self):
        return datetime.now().date().isoformat()

    def _new_state(self):
        return {"date": self._today(), "wage": 0.0, "focus_s": 0.0, "white": 0, "caught": 0,
                "speed": 1.0, "status": "working", "slack_left": 0.0, "caught_left": 0.0,
                "next_day_left": 0.0, "work_progress": 0.0, "career": career_defaults(),
                "slack_book": book_defaults()}

    def _load(self):
        state = self._new_state()
        path = self.persist_path
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as file:
                    raw = json.load(file)
                if isinstance(raw, dict):
                    state['career'] = clean_career(raw.get('career'))
                    state['slack_book'] = clean_book(raw.get('slack_book'))
                    for key in ("date", "status"):
                        if key in raw:
                            state[key] = raw[key]
                    for key in ("wage", "focus_s", "speed", "white", "caught", "work_progress", "next_day_left"):
                        if key in raw:
                            state[key] = _number(raw[key], state[key], -1e9 if key == "wage" else 0, 1e9)
            except (OSError, ValueError) as error:
                log.warning("读取上班摸鱼存档失败：%s", error)
        if state.get("date") != self._today():
            career, book = state['career'], state['slack_book']
            state = self._new_state()
            state.update(career=career, slack_book=book)
        state["status"] = state.get("status") if state.get("status") in ("working", "offwork") else "working"
        return state

    def save(self):
        if not self.persist_path:
            return
        with self._save_lock:
            with self._lock:
                state = copy.deepcopy(self.state)
                self._dirty = False
            self._write_snapshot(state)

    def set_paused(self, paused):
        with self._lock:
            self._paused = bool(paused)
            self._last_tick = time.monotonic()
            self._cues.clear()
            self._effects.clear()

    def set_away(self, away):
        """后台重启语音引擎期间挂「上厕所中」：画面不留没人却还在打字的特写。"""
        with self._lock:
            away = bool(away)
            if away and not self._away:
                self._away_since = time.monotonic()
            self._away = away

    def _roll_day(self):
        today = self._today()
        if self.state.get("date") != today:
            self._new_day(reason="新的一天")

    # ---------- 时间推进 ----------
    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            dt = now - self._last_tick
            self._last_tick = now
            if dt <= 0 or self._paused:
                return
            dt = min(dt, 5.0)
            self._roll_day()
            state = self.state
            settings = self.settings()
            self.arcade.tick(dt)
            self.play.tick(dt)
            state = self.state
            if self._poll:
                self._poll["left"] = max(0, self._poll["left"] - dt)
                if self._poll["left"] <= 0:
                    if self._poll.get("result"):
                        self._poll = None
                    else:
                        yes = sum(v == 1 for v in self._poll_votes.values())
                        no = sum(v == 2 for v in self._poll_votes.values())
                        result = "支持加薪" if yes > no else ("继续打工" if no > yes else "平票，鲨总再想想")
                        self._poll.update(result=result, left=6)
                        self._cue("poll_done", result=result, yes=yes, no=no)
            status = state.get("status", "working")
            if status == "working":
                state["focus_s"] += dt
                speed = _number(state.get("speed"), 1.0, 1.0, float(settings["max_speed"]))
                cycle = self._work_seconds(settings)
                state["work_progress"] += dt * min(float(settings['max_speed']), speed + self.play.speed_bonus()) / cycle
                rounds = int(state["work_progress"])
                if rounds:
                    state["work_progress"] -= rounds
                    amount = rounds * float(settings["work_reward"])
                    state["wage"] += amount
                    self._floater(f"+{amount:g}")
                    self.play._grant_xp(rounds * 10)
                    self.play.on_balance()
                decay = float(settings["speed_decay_per_second"]) * dt
                state["speed"] = max(1.0, speed - decay)
                self._dirty = True
                if state["wage"] >= float(settings["target"]):
                    self._go_offwork(settings)
                elif settings.get("auto_slack", True):
                    mean = max(30.0, float(settings['slack_mean_seconds']))
                    if self._auto_slack_mean != mean:
                        self._reset_auto_slack(settings)
                    self._auto_slack_left -= dt
                    if self._auto_slack_left <= 1e-6:
                        self._start_slack("working")
                else:
                    self._auto_slack_mean = None
            elif status == "slacking":
                if not self.play.inspection_pending and not self.arcade.suspended:
                    left = float(state.get("slack_left") or 0) - dt
                    state["slack_left"] = max(0.0, left)
                    if left <= 0:
                        self._finish_slack(False, settings)
                    elif random.random() < dt * float(settings["catch_rate"]):
                        if not self.play.begin_inspection():
                            self._finish_slack(True, settings)
            elif status == "caught":
                left = float(state.get("caught_left") or 0) - dt
                state["caught_left"] = max(0.0, left)
                if left <= 0:
                    state["status"] = "working"
            elif status == "offwork":
                left = float(state.get("next_day_left") or 0) - dt
                state["next_day_left"] = max(0.0, left)
                if left <= 0:
                    self._new_day(reason="第二天上班")
            self._floaters = [item for item in self._floaters if now - item["at"] < 1.8]
            if self._dirty and now - self._last_save >= 5:
                self._last_save = now
                self._dirty = False
                # 文件写入放在锁外，避免卡住采集线程；快照在下面复制。
                snapshot = True
            else:
                snapshot = None
        if snapshot is not None and self.persist_path:
            self.save()

    @staticmethod
    def _work_seconds(settings):
        return max(1.0, float(settings["base_minutes"]) * 60 * float(settings["work_reward"])
                   / float(settings["target"]))

    def _write_snapshot(self, state):
        payload = {key: state.get(key) for key in ("date", "wage", "focus_s", "white", "caught", "speed",
                                                  "status", "work_progress", "next_day_left", "career", "slack_book")}
        try:
            tmp = self.persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=1)
            os.replace(tmp, self.persist_path)
            self._last_save = time.monotonic()
        except OSError as error:
            log.warning("保存上班摸鱼存档失败：%s", error)
            with self._lock:
                self._dirty = True

    # ---------- 状态变化 ----------
    def _cue(self, kind, **extra):
        now = time.monotonic()
        if kind in ('rescue_start', 'rescue_done') and self._slack_event:
            extra.setdefault('slack_event', event_view(self._slack_event))
        self._cues.append({"type": kind, "at": now, **extra})
        sound = {'rescue_start': 'inspect', 'rules_start': 'inspect', 'poll_start': 'inspect',
                 'caught': 'scold', 'task_start': 'assign', 'rule_start': 'assign',
                 'rule_broken': 'angry', 'promotion': 'approve', 'debt_cleared': 'approve',
                 'shield_blocked': 'approve'}.get(kind)
        if kind == 'task_done':
            sound = 'approve' if self.play.pose == 'approve' else 'angry'
        if kind == 'rescue_done' and self.state['status'] != 'caught':
            sound = 'approve'
        if sound:
            self._effects.append({'sound': sound, 'at': now, 'priority': 100 if kind == 'caught' else
                                  80 if sound == 'angry' else 50 if sound == 'approve' else 20})

    def take_effect(self, max_age=2.5):
        """独立于 AI 台词队列，丢弃过时音效；同一时刻抓包音优先。"""
        now = time.monotonic()
        with self._lock:
            fresh = [cue for cue in self._effects if 0 <= now - cue['at'] <= max_age]
            self._effects.clear()
        return max(fresh, key=lambda cue: (cue['priority'], cue['at'])) if fresh else None

    def take_cue(self, max_age=12.0):
        now = time.monotonic()
        found = None
        with self._lock:
            while self._cues:
                cue = self._cues.popleft()
                if now - cue["at"] <= max_age:
                    found = cue
        return found

    def _floater(self, text, color=(120, 240, 160), x=540, y=150, size=30):
        now = time.monotonic()
        self._floaters.append({"text": text, "color": color, "x": x, "y": y, "size": size, "at": now})
        del self._floaters[:-5]
        self._dirty = True

    def _reset_auto_slack(self, settings=None):
        """抽取有上下限的工作时间间隔；默认 120 秒中心值即 60–180 秒。"""
        settings = settings or self.settings()
        self._auto_slack_mean = max(30.0, float(settings['slack_mean_seconds']))
        self._auto_slack_left = random.uniform(self._auto_slack_mean * .5, self._auto_slack_mean * 1.5)

    def _start_slack(self, reason="danmaku", event_id=None):
        if self.state.get("status") != "working":
            return False
        if event_id is not None and event_id not in SLACK_BY_ID:
            return False
        if event_id is None:
            if not self._slack_bag:
                self._slack_bag = list(SLACK_BY_ID)
                random.shuffle(self._slack_bag)
            if (len(self._slack_bag) > 1 and self._slack_event
                    and self._slack_bag[-1] == self._slack_event['id']):
                self._slack_bag[0], self._slack_bag[-1] = self._slack_bag[-1], self._slack_bag[0]
            event_id = self._slack_bag.pop()
        elif event_id in self._slack_bag:
            self._slack_bag.remove(event_id)
        self._slack_event = SLACK_BY_ID[event_id]
        self._slack_seq += 1
        settings = self.settings()
        self._slack_total = max(18.0 if event_id in INTERACTIVE else 0, float(settings["slack_seconds"]))
        self._slack_source = reason
        self.state.update(status="slacking", slack_left=self._slack_total)
        self.arcade.start(event_id)
        self._cue("slack", source=reason, slack_event=event_view(self._slack_event), slack_seq=self._slack_seq)
        self._dirty = True
        return True

    def _finish_slack(self, caught, settings=None):
        if self.state['status'] != 'slacking':
            return
        settings = settings or self.settings()
        if caught and self.play.block_catch():
            caught = False
        state = self.state
        state["white"] = int(state.get("white") or 0) + 1
        state["wage"] -= float(settings["slack_penalty"])
        state["work_progress"] = 0.0
        state["slack_left"] = 0.0
        self._floater(f"白干 +1", (255, 171, 94), x=400, y=260, size=28)
        if caught:
            state["caught"] = int(state.get("caught") or 0) + 1
            state["wage"] -= float(settings["catch_penalty"])
            state["deduction"] = float(settings["slack_penalty"]) + float(settings["catch_penalty"])
            state["status"] = "caught"
            self._caught_total = float(settings["catch_seconds"])
            state["caught_left"] = self._caught_total
            self._cue("caught", source=getattr(self, "_slack_source", "working"), slack_event=event_view(self._slack_event))
            self._floater("被抓 +1", (255, 104, 112), x=640, y=260, size=28)
        else:
            state["status"] = "working"
            self._cue("slack_done", source=getattr(self, "_slack_source", "working"), slack_event=event_view(self._slack_event))
        self.arcade.finish(caught, self._slack_event['id'])
        self.play.slack_finished(caught)
        self.play.on_balance()
        self._reset_auto_slack(settings)
        self._dirty = True

    def _go_offwork(self, settings):
        if self.state['status'] == 'offwork':
            return
        self.state.update(status="offwork", wage=float(settings["target"]), work_progress=0.0,
                          next_day_left=float(settings["next_day_seconds"]))
        if self.arcade.active:
            self.arcade.cancel()
        self.play.finish_shift()
        self._cue("goal")
        self._dirty = True

    def _new_day(self, reason=""):
        career, book = self.state['career'], self.state['slack_book']
        self.state = self._new_state()
        self.state.update(career=career, slack_book=book)
        self._floaters.clear()
        self._poll = None
        self._poll_votes.clear()
        self.play.new_day()
        self.arcade.cancel()
        self._slack_event = None
        self._auto_slack_mean = None
        self._cue("newday")
        self._dirty = True

    # ---------- 外部事件 ----------
    def on_event(self, ev):
        kind = ev.get("type")
        with self._lock:
            if self._paused:
                return
            self._roll_day()
            if kind == "danmaku":
                text = "".join(str(ev.get("text") or "").split()).lower()
                if not text:
                    return
                self._viewer = {"name": str(ev.get("name") or "观众")[:18],
                                "text": str(ev.get("text") or "")[:160], "at": time.monotonic()}
                if self.play.handle(ev, text):
                    return
                if self.arcade.handle(ev, text):
                    return
                if self._poll and not self._poll.get("result") and text in ("1", "2"):
                    # 实际 UID 优先；本机模拟事件用名字区分，不信任正文里伪造的 UID。
                    voter = str(ev.get("uid") or ev.get("name") or "匿名")
                    if voter in self._poll_votes or len(self._poll_votes) < 10000:
                        if voter not in self._poll_votes:
                            self.state['career']['wallet'] = min(1_000_000, self.state['career']['wallet'] + 1)
                            self._dirty = True
                        self._poll_votes[voter] = int(text)
                    return
                settings = self.settings()
                if text in ("紧急投票", "加薪投票", "发起投票"):
                    now = time.monotonic()
                    if (settings["poll_enabled"] and not self._poll and not self.play.round
                            and now - self._last_poll >= settings["poll_cooldown"]):
                        self._last_poll = now
                        self._poll_votes.clear()
                        self._poll = {"left": float(settings["poll_seconds"]), "result": ""}
                        self._cue("poll_start")
                    return
                status = self.state.get("status")
                if any(word in text for word in _CATCH_WORDS):
                    if status == "slacking":
                        if not self.play.begin_inspection():
                            self._finish_slack(True)
                    # 专注工作时查岗不会无故扣钱，只有摸鱼才会被抓。
                    return
                if any(word in text for word in _OFF_WORDS):
                    if status == "working" and float(self.state.get("wage") or 0) >= float(self.settings()["target"]):
                        self._go_offwork(self.settings())
                    return
                if any(word in text for word in _CLOCK_WORDS):
                    if status == "offwork":
                        self._new_day(reason="打卡")
                    return
                named_slack = SLACK_COMMANDS.get(text)
                if named_slack or any(word in text for word in _SLACK_WORDS):
                    self._start_slack("danmaku", named_slack)
                    return
                if status != "offwork" and any(word in text for word in ("加油", "涨工资", "加钱")):
                    now = time.monotonic()
                    if now - self._last_cheer >= 5:
                        self._last_cheer = now
                        self.state["wage"] = float(self.state.get("wage") or 0) + 10
                        self._floater("+10", (140, 235, 255), x=540, y=150, size=26)
                        self._dirty = True
                        self.play.on_balance()
                        if self.state["wage"] >= float(self.settings()["target"]):
                            self._go_offwork(self.settings())
                return
            if kind in ("gift", "superchat", "guard"):
                self._reward(ev, kind)

    def _reward(self, ev, kind):
        if self.state.get("status") == "offwork":
            return
        event_id = ev.get("gift_tid") or ev.get("gift_rnd") or ev.get("id")
        if event_id:
            key = (str(ev.get("room_id") or self.room_id), kind, str(event_id))
            if key in self._seen_events:
                return
            self._seen_events[key] = True
            while len(self._seen_events) > 512:
                self._seen_events.popitem(last=False)
        settings = self.settings()
        value = _number(ev.get("value") or ev.get("price") or 0, 0, 0, 1e6)
        paid = bool(ev.get("paid")) or kind in ("superchat", "guard") or value > 0
        count = int(_number(ev.get("num"), 1, 1, 10000))
        if kind == "guard":
            boost, bonus = 0.5, 300
        elif kind == "superchat":
            boost, bonus = max(0.1, value * float(settings["speed_per_yuan"])), max(50, int(value * 10))
        elif paid:
            boost, bonus = max(0.05, value * float(settings["speed_per_yuan"])), max(5, int(value * 10))
        else:
            boost, bonus = 0.02 * count, 5 * count
        self.state["speed"] = min(float(settings["max_speed"]),
                                  float(self.state.get("speed") or 1.0) + boost)
        self.state["wage"] = float(self.state.get("wage") or 0) + bonus
        self.play.on_balance()
        self._floater(f"+{bonus}", (120, 240, 160), x=540, y=150, size=32)
        if boost >= 0.1:
            self._floater(f"手速 +{boost:.1f}", (255, 171, 94), x=560, y=196, size=24)
        self._dirty = True
        if self.state["wage"] >= float(settings["target"]):
            self._go_offwork(settings)

    def set_presenter_status(self, text):
        with self._lock:
            self._presenter_status = str(text or '')

    def on_speak(self, kind, data):
        with self._lock:
            if kind == "bye":
                self._speech, self._speech_until = "", None
            elif kind == "speak":
                text = str(data.get("text") or "").strip()
                if text:
                    self._speech, self._speech_until = text, None
                    if not self._paused and time.monotonic() - self._last_frame < 2:
                        self.play.on_speech(text)
            elif kind == "speak_end":
                if self._speech:
                    self._speech_until = time.monotonic() + float(self.settings()["speech_hold"])

    # ---------- 渲染 ----------
    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            state = copy.deepcopy(self.state)
            state["target"] = float(self.settings()["target"])
            state["boss_scale"] = float(self.settings()["boss_scale"])
            state["typing_enabled"] = bool(self.settings()["typing_enabled"])
            state["focus_minutes"] = int(float(state.get("focus_s") or 0) // 60)
            state["speed"] = round(float(state.get("speed") or 1.0), 1)
            state['speed'] = round(min(float(self.settings()['max_speed']), state['speed'] + self.play.speed_bonus()), 1)
            state['office'] = self.play.snapshot()
            state['slack_play'] = self.arcade.snapshot()
            state["slack_total"] = self._slack_total
            state['slack_event'] = event_view(self._slack_event) if state['status'] in ('slacking', 'caught') else None
            state['slack_seq'] = self._slack_seq
            state['slack_elapsed'] = max(0, self._slack_total - float(state.get('slack_left') or 0))
            state["caught_total"] = self._caught_total
            state["speech"] = self._speech
            state["presenter_status"] = getattr(self, '_presenter_status', '')
            state["speech_until"] = self._speech_until
            state["floaters"] = list(self._floaters)
            state["paused"] = self._paused
            state["away"] = self._away
            state["away_elapsed"] = max(0.0, now - self._away_since) if self._away else 0.0
            state["viewer"] = dict(self._viewer) if self._viewer and now - self._viewer["at"] < 18 else None
            state["poll"] = ({**self._poll,
                              "yes": sum(v == 1 for v in self._poll_votes.values()),
                              "no": sum(v == 2 for v in self._poll_votes.values())} if self._poll else None)
            state["work_total"] = self._work_seconds(self.settings())
            state["work_left"] = max(0.0, 1 - float(state.get("work_progress") or 0)) * state["work_total"]
            state["work_reward"] = self.settings()["work_reward"]
            now_dt = datetime.now()
            state["clock"] = f"周{_WEEKDAYS[now_dt.weekday()]} {now_dt.strftime('%H:%M:%S')}"
            state["room"] = self.room_id
            state["date"] = state.get("date") or self._today()
            state['avatar_center_x'] = avatar_layout(self.settings(), state)[3]
            return state

    def frame(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            # 停止预览后没有后台涨工资；重新打开从剩余进度继续。
            if not self._last_frame or now - self._last_frame > 2:
                self._last_tick = now
            self._last_frame = now
        self.tick(now)
        snap = self.snapshot(now)
        try:
            from live_wage import WageRenderer
            if self._renderer is None or self._renderer_room != self.room_id:
                self._renderer = WageRenderer(self.room_id)
                self._renderer_room = self.room_id
            return self._renderer.render(snap, now, foreground=False)
        except Exception as error:
            log.warning("上班摸鱼画面渲染失败：%s", error)
            return self._fallback_frame(snap)

    def foreground(self, canvas):
        if self._renderer is None:
            return False
        try:
            self._renderer.apply_overlays(canvas, self.snapshot(), time.monotonic())
            return True
        except Exception as error:
            log.warning("上班摸鱼台词叠加失败：%s", error)
            return False

    def _fallback_frame(self, snap):
        import numpy as np
        image = np.zeros((1080, 1920, 4), np.uint8)
        image[..., 0] = 70
        image[..., 1] = 38
        image[..., 2] = 10
        image[..., 3] = 255
        return image


class WagePlayer:
    """主播侧循环：消费状态机 cue，用模型生成一句台词，不写死兜底句。"""

    def __init__(self, app, settings):
        self.app = app
        self._settings = settings if callable(settings) else (lambda: settings)
        self.cfg = {}
        self.running = True
        self.turns_taken = 0
        self.last_say = ""
        self._last_at = 0.0
        self._last_error_at = 0.0
        self._recent = collections.deque(maxlen=6)
        self._task = None

    def _observe(self, *_args):
        """兼容主程序在切换白饭等设置时对托管对象的广播。"""
        return None

    def _ai_cfg(self):
        cfg = dict(self.app.cfg)
        ai = dict(cfg.get("ai") or {})
        for key in ("persona", "emotional_value"):
            if self.cfg.get(key) is not None:
                ai[key] = self.cfg[key]
        if ai.get("persona") != (self.app.cfg.get("ai") or {}).get("persona"):
            ai["style"] = styles.CUSTOM
        if self.settings().get("persona") and "persona" not in self.cfg:
            ai["persona"] = self.settings()["persona"]
            ai["style"] = styles.CUSTOM
        cfg["ai"] = ai
        return cfg

    def settings(self):
        return {**DEFAULTS, **dict(self._settings() or {})}

    def stop(self):
        self.running = False
        self.app.wage.set_paused(True)

    def start(self):
        import asyncio
        self.app.wage.set_paused(False)
        self._task = asyncio.create_task(self.run_forever())
        return self._task

    async def run_forever(self):
        import asyncio
        self.app.push("log", {"level": "info", "msg": "上班摸鱼托管已启动，工资和弹幕互动开始计时"})
        try:
            await self._wait_engine()
            if not self.running:
                return
            await self._speak({"type": "start"}, force=True)
            while self.running:
                await asyncio.sleep(1)
                try:
                    if not self.settings().get("narrate", True):
                        continue
                    cue = self.app.wage.take_cue()
                    idle = self.settings()["idle_line_seconds"]
                    if cue is not None:
                        if await self._speak(cue):
                            continue
                    elif time.monotonic() - self._last_at >= idle:
                        await self._speak({"type": "idle"})
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.app.push("log", {"level": "warn", "msg": f"上班摸鱼台词生成失败：{error}"})
                    await asyncio.sleep(5)
        finally:
            self.running = False
            self.app.wage.set_presenter_status('')

    async def _wait_engine(self):
        import asyncio
        if not self.app.cfg.get("tts", {}).get("enabled", True):
            return
        ready = getattr(self.app, "engine_ready", None)
        if ready is not None and not ready.is_set():
            self.app.wage.set_presenter_status('语音准备中，就绪后会自动开口…')
            self.app.push("log", {"level": "info", "msg": "等语音引擎就绪…"})
            try:
                await asyncio.wait_for(ready.wait(), 180)
            except asyncio.TimeoutError:
                self.app.wage.set_presenter_status('语音尚未就绪，请检查语音引擎')
                self.app.push("log", {"level": "warn", "msg": "语音引擎等待超时，上班摸鱼先开画面"})
            else:
                self.app.wage.set_presenter_status('')

    def _prompt(self, cue):
        state = self.app.wage.snapshot()
        status = {"working": "正在专心工作", "slacking": "正在摸鱼", "caught": "刚被老板抓包",
                  "offwork": "已经下班"}.get(state.get("status"), "正在工作")
        kind = cue.get("type")
        activity = cue.get('slack_event') or state.get('slack_event')
        activity_context = (f"本轮摸鱼是{activity['title']}：{activity['scene']}。借口灵感：{activity['excuse']}。\n"
                            if activity else '')
        slack_origin = '你自己忍不住开始摸鱼' if cue.get('source') == 'working' else '观众怂恿你开始摸鱼'
        task = {
            "start": "你刚开播，跟观众打个招呼，说一句今天要打工摸鱼的话。",
            "slack": f"{slack_origin}，围绕本轮摸鱼内容说一句心虚又调皮的即时反应，不要编造观众发过的话。",
            "caught": "你摸鱼被老板抓到了，刚白干，说一句被抓包的反应。",
            "slack_done": "你摸完鱼没被抓，但时间白干了，说一句自嘲的话。",
            'slack_game_done': f"弹幕小游戏结算：{cue.get('result', '')}。按真实结果回应，不要把公共积分说成工资。",
            "goal": "今日工资到账，可以下班了，说一句如释重负的话。",
            "newday": "第二天上班打卡，说一句重新开始打工的话。",
            "idle": "冷场了，顺着当前的打工状态自然说一句。",
            "poll_start": "鲨总发起了加薪投票，观众发1支持加薪、发2让你继续打工，招呼大家投票。",
            "poll_done": f"投票结束，结果是{cue.get('result', '')}，对结果做一句反应，不要声称工资已经改变。",
            'rescue_start': '鲨总快来查岗了，请观众投票帮你装开会或疯狂打字，也可能有人打小报告。',
            'rescue_done': f"查岗结果：{cue.get('result', '')}，根据真实状态说一句临场反应。",
            'task_start': f"鲨总派了一个离谱任务：{cue.get('title', '')}。招呼观众发1/2/3选办法。",
            'task_done': f"任务结果：{cue.get('result', '')}，自然吐槽一句。",
            'rules_start': '观众正在投票改员工守则，招呼大家选择。',
            'rule_start': f"新守则生效：{cue.get('rule', '')}，从这句开始就要遵守。",
            'rule_broken': '你刚说漏嘴违反守则，被扣了60工资，按当前规则说一句心虚反应。',
            'rule_done': '守则挑战完成，没有说漏嘴，工资加80，说一句得意的话。',
            'item_bought': f"公共道具柜多了一个{cue.get('item', '')}，轻松回应。",
            'item_used': f"观众使用了{cue.get('item', '')}，根据道具效果说一句。",
            'shield_blocked': '挡箭牌替你挡住一次查岗罚款，说一句庆幸的话。',
            'pillow_paid': '枕头让你带薪摸鱼成功，多拿30工资，说一句开心的话。',
            'promotion': f"你升职成了{cue.get('rank', '')}，说一句符合人设的反应。",
            'debt_start': f"你开始倒欠公司{cue.get('debt', 0)}工资，进入还债剧情，吐槽一句。",
            'debt_cleared': '你终于还清倒欠公司的工资，说一句如释重负的话。',
        }.get(kind, "顺着当前的打工状态自然说一句。")
        return (
            "你正在B站直播一档《上班摸鱼》互动节目：你扮演一位坐在工位上的虚拟主播，"
            "观众用弹幕让你摸鱼、用礼物给你加工资和手速。\n"
            f"当前状态：{status}；今日工资 {int(state.get('wage') or 0)}/{int(state.get('target') or 6000)}；"
            f"专注 {int(state.get('focus_minutes') or 0)} 分钟；白干 {int(state.get('white') or 0)} 次；"
            f"被抓 {int(state.get('caught') or 0)} 次；手速 ×{float(state.get('speed') or 1):.1f}。\n"
            + activity_context
            + self.app.wage.play.context() + '\n'
            + self.app.wage.arcade.context() + '\n'
            +
            f"现在{task}\n"
            "只输出主播要说出口的一小段口语，不超过30个字，不要动作描写、不要解释、不要报流水账。"
        )

    async def _speak(self, cue, force=False):
        settings = self.settings()
        if not self.running or not settings.get("narrate", True):
            return False
        # 观众弹幕触发的摸鱼/抓包，聊天回复已经会回应它，不再叠一句。
        if cue.get("source") == "danmaku" and cue.get("type") in ("slack", "caught", "slack_done"):
            return False
        now = time.monotonic()
        important = cue.get('type') in ('slack', 'slack_game_done', 'rescue_start', 'rescue_done', 'task_start', 'task_done',
                                         'rule_start', 'rule_broken', 'promotion', 'debt_cleared')
        cooldown = 0 if force else (1.5 if important else max(12.0, float(settings["idle_line_seconds"]) * 0.35))
        if not force and now - self._last_at < cooldown:
            return False
        try:
            text, emotion = await self.app.brain.ai(self._ai_cfg(), self._prompt(cue))
        except Exception as error:
            self._last_at = time.monotonic()
            self.app.wage.set_presenter_status('台词生成失败，请检查 AI 设置')
            if time.monotonic() - self._last_error_at > 300:
                self._last_error_at = time.monotonic()
                self.app.push("log", {"level": "warn", "msg": f"上班摸鱼台词生成失败：{error}"})
            return False
        text = (text or "").strip()
        if not self.running or self.app.wage.snapshot()['paused']:
            return False
        if cue.get('type') in ('slack', 'slack_game_done'):
            live = self.app.wage.snapshot()
            if cue.get('slack_seq') != live['slack_seq']:
                return False
            if cue['type'] == 'slack' and live['status'] != 'slacking':
                return False
            if cue['type'] == 'slack_game_done' and cue.get('result') != (live['slack_play']['result'] or {}).get('text'):
                return False
        current = self.app.wage.play.round
        if cue.get('type') in ('rescue_start', 'task_start'):
            expected = 'rescue' if cue['type'] == 'rescue_start' else 'task'
            if not current or current['kind'] != expected:
                return False
        if not text or text in self._recent:
            self._last_at = time.monotonic()
            return False
        self._recent.append(text)
        self.app.wage.set_presenter_status('')
        self.last_say = text
        self.turns_taken += 1
        self._last_at = time.monotonic()
        if cue.get("type") in ("caught", "slack"):
            self.app.pipe.react(text, emotion)
        else:
            self.app.pipe.narrate_fresh(text, emotion)
        return True

    def fresh_commentary(self):
        if self.last_say and time.monotonic() - self._last_at <= 20:
            return self.last_say
        return ""

    def remember_interaction(self, event, reply):
        # Pipeline 已维护观众的对话历史；这里只用于避免主动台词重复刚念过的回复。
        self._recent.append(str(reply or ""))

    def commentary_context(self):
        """Pipeline 回复真实弹幕时读取同一份节目状态。"""
        state = self.app.wage.snapshot()
        poll = state.get("poll")
        voting = (f"\n正在投票：支持加薪 {poll['yes']} 票、继续打工 {poll['no']} 票；观众发1/2参与。"
                  if poll else "")
        event = state.get('slack_event')
        activity = (f"\n本轮摸鱼是{event['title']}：{event['scene']}。借口灵感：{event['excuse']}。" if event else '')
        return ("正在直播《上班摸鱼》，分区为虚拟主播/虚拟日常。你是工位上的打工主播，老板叫鲨总。\n"
                + self.status_text()
                + f"；本轮工作还剩 {math.ceil(state['work_left'])} 秒，完成获得 {state['work_reward']:g} 工资。\n"
                "摸鱼会白干当前一轮；摸鱼时查岗会被抓并扣工资，工资允许为负数（倒欠公司）。"
                "观众打字说送礼物只是聊天，只有平台真实礼物事件才会增加数值。"
                "回应观众原话，可围绕写代码、工资、摸鱼和鲨总自然吐槽；不要声称执行了关机或删文件。" + activity + voting
                + '\n' + self.app.wage.play.context() + '\n' + self.app.wage.arcade.context())

    def status_text(self):
        state = self.app.wage.snapshot()
        label = {"working": "上班中", "slacking": "摸鱼中", "caught": "被抓了", "offwork": "已下班"}.get(
            state.get("status"), "上班中")
        if state.get('status') == 'slacking' and state.get('slack_event'):
            label = state['slack_event']['label']
        if state.get("away"):
            label = "上厕所中"
        return (f"{label} · 工资 {int(state.get('wage') or 0)}/{int(state.get('target') or 6000)} · "
                f"白干 {int(state.get('white') or 0)} · 被抓 {int(state.get('caught') or 0)} · "
                f"手速 ×{float(state.get('speed') or 1):.1f}")

    def status_summary(self):
        state = self.app.wage.snapshot()
        return {"kind": "wage", "status": state.get("status"), "wage": round(float(state.get("wage") or 0), 1),
                "target": state.get("target"), "focus_minutes": state.get("focus_minutes"),
                "white": state.get("white"), "caught": state.get("caught"),
                "speed": state.get("speed"), "say": self.last_say}


def preview_jpeg(room_id=0):
    from live_wage import preview_jpeg as render_preview
    return render_preview(room_id)
