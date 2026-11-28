"""Physics and audience interaction for 大肥鱼爬出海."""

from collections import deque
from dataclasses import dataclass
import math
import random
import time
from threading import RLock

from climb_contact import Contact, carry_blend, carry_tip, cup_contact
from climb_course import (BASE_Y, CHAPTERS, CHECKPOINTS, LEDGES, WATER_Y, Ledge, chapter_at,
                          ledge_indices, obstacle_indices)
from climb_weather import (CLEAR, FIRST_WEATHER_SECONDS, FIRST_WEATHER_TURN, WEATHERS,
                           WEATHER_BLEND, WEATHER_SECONDS, WEATHER_TURNS, pick_weather)


TITLE = "大肥鱼爬出海"
WIDTH, HEIGHT = 1280, 720
GRAVITY = 680
STEP = 1 / 120
GUST_DELAY = 43
ROUND_BREAK = 7.0
MIN_SPEED, MAX_SPEED = 250, 675
BODY_RADIUS = 26
START_X = 430
BRACE_TIME = 2.86
OPENING_BRACE_TIME = 4.89
TERRAIN_BRACE_TIME = {"折返栈道": 2.71, "贴壁窄台": 3.03, "断崖横沟": 3.49,
                      "错层挑檐": 3.11, "长台转角": 2.53}
REACTION_LIFETIME = 10
GRIP_X, GRIP_Y = 66, 109
CARRY_POSES = ((-0.46, 0.78), (-1.52, 0.74), (-2.65, 0.66), (2.28, 0.64), (1.03, 0.57),
               (0.12, 0.33), (1.10, 0.33), (-1.52, 0.33), (2.90, 0.33), (2.95, 0.10))
TIGHT_CARRY_POSES = CARRY_POSES + tuple((angle, length)
                                       for angle in (-2.5, -1.5, -0.5, 0.5, 1.5, 2.5)
                                       for length in (0.10, 0.24))
HELP_GIFTS = {"boat": "打捞艇", "step": "泡泡踏板", "school": "鱼群跟拍"}
POWER_GIFTS = {"rocket": "火箭助推", "magnet": "磁力吸盘", "feather": "金羽毛"}
SABOTAGE_GIFTS = {"oil": "吸盘打滑", "squall": "逆风推歪"}
GIFTS = HELP_GIFTS | POWER_GIFTS | SABOTAGE_GIFTS
FOLLOW_EFFECTS = {"nitro": "氮气加速"}
NITRO_BURN = 0.9
NITRO_LIFT = 980
NITRO_RISE = 520
FOLLOW_COOLDOWN = 60
ROCKET_BURN = 1.3
ROCKET_LIFT = 1180
ROCKET_RISE = 720
MAGNET_JUMPS = 3
FALL_REACH = 220
DEFAULTS = {"guidance": 1, "air": 2, "net": 3, "narrate": True, "weather": True,
            "avatar_enabled": True, "avatar_x": 0.16, "avatar_y": 0.88,
            "avatar_size": 0.42, "avatar_zoom": 2.0,
            **{kind + "_gift": "" for kind in GIFTS}}


COMMANDS = {"指路": "guidance", "打气": "air", "接住": "net"}

@dataclass(frozen=True, slots=True)
class VoiceCue:
    kind: str
    fact: str
    emotion: str
    expires: float
    epoch: int
    attempt: tuple[int, int] | None = None
    at_ledge: int | None = None


@dataclass(frozen=True, slots=True)
class Brace:
    body: tuple[float, float]
    hit: Contact
    held_from: float
    facing: int


@dataclass(frozen=True, slots=True)
class RimGrab:
    hit: Contact
    index: int
    from_grip: tuple[float, float]
    start: float


def validate_settings(values):
    if not isinstance(values, dict) or set(values) - DEFAULTS.keys():
        raise ValueError("攀爬互动设置无效")
    for key in COMMANDS.values():
        if key in values and (type(values[key]) is not int or not 1 <= values[key] <= 20):
            raise ValueError("弹幕人数须在 1–20 之间")
    names = []
    for gift in GIFTS:
        value = values.get(gift + "_gift", "")
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError("礼物名称最长 40 字")
        if value.strip():
            names.append(value.strip())
    if len(set(names)) != len(names):
        raise ValueError("每种攀爬效果需配置不同的礼物")
    if "narrate" in values and type(values["narrate"]) is not bool:
        raise ValueError("攀爬语音开关无效")
    if "weather" in values and type(values["weather"]) is not bool:
        raise ValueError("攀爬天气开关无效")
    if "avatar_enabled" in values and type(values["avatar_enabled"]) is not bool:
        raise ValueError("皮套显示开关无效")
    for key, low, high in (("avatar_x", 0.0, 1.0), ("avatar_y", 0.0, 1.0),
                           ("avatar_size", 0.2, 1.4), ("avatar_zoom", 1.0, 3.0)):
        if key not in values:
            continue
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError("皮套位置或大小超出范围")


def clamp(value, low, high):
    return max(low, min(high, value))


def height(y):
    return max(0, round((BASE_Y - y) / 25))


def direction(x, y):
    length = math.hypot(x, y)
    return (x / length, y / length) if length > 1 else (0.7, -0.714)


class Climb:
    def __init__(self, settings):
        self.settings = settings
        self.lock = RLock()
        self.watchers = 0
        self.gift_ids = {}
        self.follow_ids = {}
        self.round_number = 1
        self.voice_epoch = 0
        self.auto = True
        self.effects = {}
        self.reset()

    def reset(self, *, next_round=False):
        with self.lock:
            self.round_number = self.round_number + 1 if next_round else 1
            self.voice_epoch += 1
            pending = {}
            for gift, effect in self.effects.items():
                if gift not in GIFTS or effect.get("used") or effect.get("jumps", 1) <= 0:
                    continue
                if gift == "step":
                    pending[gift] = {"by": effect["by"]}
                else:
                    pending[gift] = effect.copy()
            self.run_clock = 0.0
            self.accumulator = 0.0
            self.x, self.y = float(START_X), float(BASE_Y)
            self.vx = self.vy = 0.0
            self.facing = 1
            self.camera_x = self.camera_y = 0.0
            self.on_ledge = self.last_ledge = self.record_ledge = self.checkpoint = 0
            self.best_y = self.y
            self.deepest_fall = self.turns = 0
            self.charge_at = None
            self.jets = 0
            self.nitro_until = 0.0
            self.rocket_until = 0.0
            self.magnet_flight = False
            self.finished = False
            self.result_left = 0.0
            self.auto_at = 1.1
            self.landed_at = 0.0
            self.brace = None
            self.rim_grab = None
            self.release_angle = -0.8
            self.rod_length = 134
            self.carry_pose = CARRY_POSES[0]
            self.cup_at = None
            self.cup_block = None
            self.cup_cooldown = 0.0
            self.last_impact = None
            self.rod_hits = 0
            self.wind = 0
            self.gust = self.planned_gust = 0
            self.gift_wind = 0
            self.flight_steps = 0
            self.flight_since = 0.0
            self.stuck_tries = 0
            self.seen_gusts = set()
            self.misses = 0
            self.slips = 0
            self.learned_grips = set()
            self.comeback_at = None
            self.comebacks = 0
            self.auto_plan = None
            self.flight_target = None
            self.replan = False
            self.reposition_to = None
            self.reposition_attempt = 0
            self.boost_at = 0.0
            self.recover_at = 0.0
            self.recover_to = 0
            self.boat_riding = False
            self.effects = pending
            self.votes = {key: {} for key in COMMANDS.values()}
            self.cooldowns = {}
            self.news = deque(maxlen=3)
            self.voice = None
            self.opening_voice = True
            self.last_operation_voice = None
            self.weather = CLEAR
            self.weather_prev = CLEAR
            self.weather_since = 0.0
            self.weather_until = FIRST_WEATHER_TURN
            self.weather_until_clock = FIRST_WEATHER_SECONDS
            self.weather_rng = random.Random(20261127 + self.round_number * 7919)

    def set_auto(self, enabled):
        with self.lock:
            self.auto = bool(enabled)

    def weather_enabled(self):
        return self.settings().get("weather", DEFAULTS["weather"]) is not False

    def _active_weather(self):
        return self.weather if self.weather_enabled() else CLEAR

    def set_weather(self, key, turns=None):
        with self.lock:
            weather = WEATHERS.get(key)
            if weather is None:
                return False
            self._apply_weather(weather, turns=turns)
            return True

    def _apply_weather(self, weather, *, turns=None, announce=True):
        if weather.key != self.weather.key:
            self.weather_prev = self.weather
        else:
            self.weather_prev = weather
        self.weather = weather
        self.weather_since = self.run_clock
        self.weather_until = self.turns + (turns if turns is not None
                                           else self.weather_rng.randint(*WEATHER_TURNS))
        self.weather_until_clock = self.run_clock + self.weather_rng.randint(*WEATHER_SECONDS)
        if announce and not self.finished:
            self._notice(weather.notice)
            self._cue(weather.fact, weather.emotion)

    def _rotate_weather(self):
        if self.finished or not self.weather_enabled() or self.turns < self.weather_until:
            return
        self._apply_weather(pick_weather(self.weather_rng, LEDGES[self.last_ledge].chapter,
                                         exclude=self.weather.key))

    def _expire_weather(self):
        if self.finished or not self.weather_enabled() or self.run_clock < self.weather_until_clock:
            return
        self._apply_weather(pick_weather(self.weather_rng, LEDGES[self.last_ledge].chapter,
                                         exclude=self.weather.key))

    def _power(self):
        if self.charge_at is None:
            return 0.0
        return clamp((self.run_clock - self.charge_at) / 0.94, 0.08, 1)

    def _target(self):
        index = min(len(LEDGES) - 1, self.last_ledge + 1)
        ledge = LEDGES[index]
        margin = min(90, ledge.width * 0.36)
        return index, clamp(self.x, ledge.x + margin, ledge.right - margin)

    def _launch(self, aim, power, pose):
        if self.on_ledge is None or self.brace is None:
            self.charge_at = None
            return
        speed = MIN_SPEED + (MAX_SPEED - MIN_SPEED) * power
        hit = self.brace.hit
        # The anchored cup provides a normal reaction and a tangential grip.
        push, pull = self._contact_forces(aim[0] * speed, aim[1] * speed)
        self.vx = hit.nx * push - hit.ny * pull
        self.vy = hit.ny * push + hit.nx * pull
        self.release_angle, self.rod_length = self._release_pose()
        self.carry_pose = pose
        self.cup_at = hit.point
        self.cup_block = None
        self.cup_cooldown = self.run_clock
        self.brace = None
        self.on_ledge = None
        self.flight_steps = 0
        self.flight_since = self.run_clock
        self.stuck_tries = 0
        self.charge_at = None
        self.jets = 1 + int(self.effects.pop("air", None) is not None)
        self.boost_at = self.run_clock + 0.36
        self.replan = False
        magnet = self.effects.get("magnet")
        if magnet:
            self.magnet_flight = True
            magnet["jumps"] -= 1
            if magnet["jumps"] <= 0:
                self.effects.pop("magnet")
        else:
            self.magnet_flight = False
        prank = next((kind for kind in self.effects if kind in SABOTAGE_GIFTS), None)
        if magnet and prank == "oil":
            self.effects.pop("oil", None)
            prank = None
        natural_slip = not magnet and hit.ledge.slick and self.last_ledge not in self.learned_grips
        if natural_slip:
            self.learned_grips.add(self.last_ledge)
        weather = self._active_weather()
        slipped = False
        if prank == "oil":
            by = self.effects.pop(prank)["by"]
            self._slip(hit, 0.39, f"{by} 的礼物让吸盘打滑", "观众的捣乱礼物让吸盘打滑，刚离开岩面就几乎失去了向上的力")
            slipped = True
        elif natural_slip and "guidance" not in self.effects:
            self._slip(hit, 0.34, "吸盘打滑 · 落点偏离", "吸盘在湿岩上打滑，起跳偏离预定落点")
            slipped = True
        elif (weather.slip and not magnet and "guidance" not in self.effects
                and self.last_ledge not in self.learned_grips
                and self.weather_rng.random() < weather.slip):
            self.learned_grips.add(self.last_ledge)
            self._slip(hit, weather.slip_lift, f"{weather.name}打滑 · 落点偏离", weather.slip_fact)
            slipped = True
        else:
            if natural_slip:
                self._notice("弹幕指路 · 稳住湿滑支点")
                self._cue("观众的指路灯帮忙避开了湿滑支点，安全起跳", "开心")
            if prank == "squall":
                self.gift_wind = math.copysign(470, -aim[0])
        if weather.drift and not slipped and self.flight_target is not None:
            drift = self.weather_rng.uniform(-weather.drift, weather.drift)
            self.vx += drift
            if abs(drift) >= weather.drift * 0.35:
                self.replan = True
                self._notice("雾里看偏了 · 落点需要修正")
        self.turns += 1
        if self.effects.get("school", {}).get("jumps", 0) == 0:
            self.effects.pop("school", None)
        if "school" in self.effects:
            self.effects["school"]["jumps"] -= 1

    def _slip(self, hit, lift, notice, fact):
        # The cup slides sideways before it can transfer the planned lift.
        self.vx *= 1.16
        self.vy *= lift
        self.replan = True
        self.boost_at = self.run_clock + 0.55
        self._record_rod_hit(hit)
        self._notice(notice)
        self._cue(fact, "惊讶")
        self.slips += 1

    def _boost(self, dx, dy, impulse=150):
        # A last-second reach can latch only if the cup actually strikes its rim.
        for index in ledge_indices(self.y - 108, self.y + 45):
            ledge = LEDGES[index]
            if self.flight_target is not None and ledge is not self.flight_target:
                continue
            edge = ledge.x if dx > 0 else ledge.right
            if 18 < (edge - self.x) * (1 if dx > 0 else -1) < 110 and -30 < self.y - ledge.y < 82:
                grip = self._grip()
                reach = (edge + (9 if dx > 0 else -9), ledge.surface(edge) + 8)
                if math.dist(grip, reach) <= 158:
                    start = self.cup_at or grip
                    hit = cup_contact(start, reach)
                    if hit and hit.ledge is ledge and hit.y <= ledge.surface(edge) + 17:
                        self._hold_rim(index, hit)
                        return
        self.vx += dx * impulse
        self.vy += dy * impulse
        self.jets -= 1
        self.boost_at = self.run_clock + 0.28

    def _hold_rim(self, index, hit):
        self.rim_grab = RimGrab(hit, index, self._grip(), self.run_clock)
        self.cup_at = hit.point
        self.cup_block = hit
        self.vx = self.vy = 0.0
        self._record_rod_hit(hit)
        self._notice("吸盘勾到了岩沿")
        if self.replan:
            self._cue("即将掉落时吸盘勾住岩沿，正在靠杆把自己拉上去", "惊喜")

    def _pull_up(self):
        grab = self.rim_grab
        hit = grab.hit
        ledge = hit.ledge
        gx, gy = grab.from_grip
        edge_distance = min(ledge.width * 0.36, BODY_RADIUS + 12)
        target_x = clamp(hit.x, ledge.x + edge_distance, ledge.right - edge_distance)
        target_grip = (target_x + self.facing * GRIP_X, ledge.surface(target_x) - GRIP_Y)
        blend = clamp((self.run_clock - grab.start) / 0.54, 0, 1)
        eased = blend * blend * (3 - 2 * blend)
        start_angle = math.atan2(gy - hit.y, gx - hit.x)
        final_angle = math.atan2(target_grip[1] - hit.y, target_grip[0] - hit.x)
        angle = start_angle + math.remainder(final_angle - start_angle, 2 * math.pi) * eased
        start_radius = math.hypot(gx - hit.x, gy - hit.y)
        target_radius = math.dist(hit.point, target_grip)
        radius = start_radius + (target_radius - start_radius) * eased
        offset_x, offset_y = self._grip_offset()
        self.x = hit.x + math.cos(angle) * radius - offset_x
        self.y = hit.y + math.sin(angle) * radius - offset_y
        if blend >= 1:
            self.x = target_x
            self._land(grab.index, ledge)

    def _land(self, index, ledge):
        if self.flight_target is not None and ledge.y > self.flight_target.y + 20:
            self.misses += 1
            self.comeback_at = self.record_ledge
            if not self.recover_at:
                if index < self.last_ledge and index >= 0:
                    lost = height(LEDGES[self.last_ledge].y) - height(ledge.y)
                    self._notice(f"掉回下层 · 少爬了 {lost} 米")
                    self._cue(f"刚刚失手，掉回下层，倒退了{lost}米", "尴尬")
                else:
                    self._notice("没够到下一层 · 重新摆杆")
                    self._cue("刚刚跳出去却没够到目标，落回低处重试", "尴尬")
        self.x = clamp(self.x, ledge.x + BODY_RADIUS, ledge.right - BODY_RADIUS)
        self.y = ledge.surface(self.x)
        self.vx = self.vy = 0.0
        self.on_ledge = index
        self.landed_at = self.run_clock
        hold = TERRAIN_BRACE_TIME.get(ledge.terrain, BRACE_TIME)
        if index < 0:
            hold = 3.5
        elif index <= 3:
            hold = OPENING_BRACE_TIME
        elif ledge.material in ("snow", "steel"):
            hold += 0.28
        if self.weather_enabled():
            hold = max(0.9, hold + self._active_weather().brace)
        self.auto_at = self.run_clock + hold
        self.flight_since = 0.0
        self.stuck_tries = 0
        self.brace = None
        self.rim_grab = None
        self.cup_at = None
        self.cup_block = None
        self.auto_plan = None
        self.flight_target = None
        self.gift_wind = 0
        self.magnet_flight = False
        self.reposition_to = None
        self.reposition_attempt = 0
        if index < 0:
            return
        self.last_ledge = index
        if index > self.record_ledge:
            previous_landmark = LEDGES[self.record_ledge].landmark
            self.record_ledge = index
            checkpoint = CHECKPOINTS[ledge.chapter]
            if checkpoint > self.checkpoint:
                self.checkpoint = checkpoint
                self._cue(f"第一次抵达{LEDGES[checkpoint].section}，继续向上攀爬", "开心")
            if self.comeback_at is not None and index > self.comeback_at:
                self.comeback_at = None
                self._notice("翻回来了 · 刷新最高落脚点")
                self._cue("刚把失手丢掉的高度爬回来，还刷新了最高落脚点", "开心")
                self.comebacks += 1
            elif ledge.landmark and ledge.landmark != previous_landmark:
                self._notice(f"新地形 · {ledge.landmark}")
                self._cue(f"刚踩上新地标{ledge.landmark}的第一个落脚点", "惊喜")
            if index == len(LEDGES) - 1:
                self.finished = True
                self.result_left = ROUND_BREAK
                self._cue("已经站上峰顶并通关", "开心")
        self._rotate_weather()

    def _cue(self, fact, emotion):
        self.voice = VoiceCue("reaction", fact, emotion, self.run_clock + REACTION_LIFETIME, self.voice_epoch)

    def _notice(self, line):
        self.news.append((self.run_clock + 6, line))

    def take_voice(self):
        with self.lock:
            if not self.auto:
                return None
            if self.opening_voice and not self.finished:
                self.opening_voice = False
                return VoiceCue("opening", "本局刚开始解说，目标是坐着盆靠吸盘杆爬上山顶", "开心",
                                self.run_clock + REACTION_LIFETIME, self.voice_epoch)
            voice, self.voice = self.voice, None
            if voice and self.run_clock < voice.expires:
                return voice
            return None

    def take_operation_voice(self):
        """Describe the current approach, or plan one ledge ahead on short approaches."""
        with self.lock:
            attempt = (self.turns, self.reposition_attempt)
            if (not self.auto or self.finished or self.recover_at or self.on_ledge is None
                    or attempt == self.last_operation_voice):
                return None
            if self.reposition_to is not None:
                delta = self.reposition_to - self.x
                remaining = abs(delta) / 112
                side = "右" if delta > 0 else "左"
                fact = f"正在往{side}移到新的支点，准备换个角度发力"
            else:
                remaining = self.auto_at - self.run_clock
                index, landing_x = self._target()
                delta = landing_x - self.x
                aim = "正上方" if abs(delta) < 15 else "右上" if delta > 0 else "左上"
                ledge = self._rock_underfoot()
                target = LEDGES[index]
                fact = f"准备撑杆跳往{aim}的{target.landmark or target.surface_name}"
                if "air" in self.effects:
                    fact += "，有一口弹幕给的备用气，必要时在半空使用"
                elif ledge and ledge.slick and self.last_ledge not in self.learned_grips:
                    fact += "，脚下是湿岩，吸盘有打滑风险，还没有失手"
                elif ledge and ledge.slick and self.comeback_at is not None:
                    fact += "，回到刚打滑过的那块岩石，准备这次先压稳吸盘"
                elif target.terrain == "断崖横沟":
                    fact += "，中间跨度很大"
                elif target.width < 120:
                    fact += "，落脚点很窄，需要瞄正中间"
            if remaining < 1.25:
                return None
            self.last_operation_voice = attempt
            if (remaining < 3.8 and self.reposition_to is None and self.on_ledge >= 0
                    and self.last_ledge < len(LEDGES) - 2):
                footing_index = self.last_ledge + 1
                footing, destination = LEDGES[footing_index:footing_index + 2]
                aim = "右上" if destination.center > footing.center else "左上"
                fact = (f"脚下落脚点在{footing.landmark or footing.surface_name}，"
                        f"正在考虑跳向{aim}的{destination.landmark or destination.surface_name}")
                if footing.piece == "tile":
                    fact += "，脚下是一片斜瓦"
                if destination.width < 120:
                    fact += "，前方落脚面偏窄"
                return VoiceCue("operation", fact, "", self.run_clock + 13, self.voice_epoch,
                                (self.turns + 1, 0), footing_index)
            return VoiceCue("operation", fact, "", self.run_clock + remaining, self.voice_epoch, attempt)

    def voice_remaining(self, cue):
        with self.lock:
            remaining = cue.expires - self.run_clock
            if not self.auto or cue.epoch != self.voice_epoch or remaining <= 0:
                return 0
            if cue.at_ledge is not None:
                turn = cue.attempt[0]
                if self.turns < turn or (self.turns == turn and self.on_ledge is None):
                    return None
                if self.on_ledge != cue.at_ledge:
                    return 0
                remaining = min(remaining, self.auto_at - self.run_clock)
            if cue.attempt and cue.attempt != (self.turns, self.reposition_attempt):
                return 0
            return max(0, remaining)

    def _fall(self):
        self.comeback_at = self.record_ledge
        self.flight_since = 0.0
        self.stuck_tries = 0
        self.y = WATER_Y
        self.vx = self.vy = 0.0
        self.recover_at = self.run_clock + 1.45
        feather = self.effects.pop("feather", None)
        boat = None if feather else self.effects.pop("boat", None)
        self.boat_riding = bool(boat)
        if feather:
            self.recover_to = self.record_ledge
            self._notice(f"{feather['by']} 的金羽毛把大肥鱼拉回最高点")
            self._cue("摔进水里时观众的金羽毛亮起，把角色直接捞回最高岩沿", "惊喜")
        elif boat:
            self.recover_at += 0.85
            self.recover_to = self.last_ledge
            self._notice(f"{boat['by']} 的打捞艇出发了")
            self._cue("跌落时观众的打捞艇赶来把角色捞回上一块岩石", "惊喜")
        else:
            self.recover_to = 0
            self._notice("落水到底 · 从旧船坞重爬")
            self._cue("失手一路掉进山脚的水里，要从最底层的旧船坞重新爬", "尴尬")

    def _auto(self):
        if self.on_ledge is None or self.brace is None or self.run_clock < self.auto_at:
            return
        if self.run_clock < self.brace.held_from + 0.25:
            return
        if self.auto_plan is None:
            index, landing_x = self._target()
            target = route_target = LEDGES[index]
            route_x = landing_x
            plank = self.effects.get("step")
            if plank and "ledge" not in plank and self.on_ledge >= 0:
                x = self.x + (landing_x - self.x) * 0.54
                y = round((self.y + target.y) / 2)
                plank.update(ledge=Ledge(round(x) - 65, y, 130, 0), until=self.run_clock + 22)
            if plank and not plank.get("used") and "ledge" in plank and self.y > plank["ledge"].y + 32:
                target = plank["ledge"]
                landing_x = target.center
            weather = self._active_weather()
            self.planned_gust = (self.gust if weather.gust or self.last_ledge in self.seen_gusts
                                 or "guidance" in self.effects else 0)
            shot = self._select_shot(target, landing_x)
            if shot is None and target is not route_target:
                target, landing_x = route_target, route_x
                shot = self._select_shot(target, landing_x)
            if shot is None:
                self._start_reposition()
                return
            vx, vy, speed, pose = shot
            power = (speed - MIN_SPEED) / (MAX_SPEED - MIN_SPEED)
            self.auto_plan = (direction(vx, vy), power, pose)
            self.flight_target = target
            self.charge_at = self.run_clock
        aim, power, pose = self.auto_plan
        if self._power() >= power:
            self._launch(aim, power, pose)
            self.auto_plan = None

    def _start_reposition(self):
        ledge = self._rock_underfoot()
        if ledge is None:
            return
        spans = (0.50, 0.20, 0.80, 0.36, 0.64)
        self.reposition_to = clamp(ledge.x + ledge.width * spans[self.reposition_attempt % len(spans)],
                                   ledge.x + BODY_RADIUS, ledge.right - BODY_RADIUS)
        self.reposition_attempt += 1
        self.brace = None

    def _reposition(self, ledge):
        delta = self.reposition_to - self.x
        self.x += clamp(delta, -112 * STEP, 112 * STEP)
        self.y = ledge.surface(self.x)
        self._seek_cup(ledge)
        if abs(delta) < 1.5:
            self.reposition_to = None
            self.landed_at = self.run_clock - 1.8
            self.auto_at = self.run_clock + 0.44

    def _select_shot(self, target, landing_x):
        options = []
        left, right = target.landing_bounds(260)
        aims = (landing_x, target.center, left + 4, right - 4)
        times = (i / 32 for i in range(18, 52))
        poses = CARRY_POSES
        min_descent = 54
        if self.reposition_attempt:
            aims += (left * 0.75 + right * 0.25, left * 0.25 + right * 0.75)
            times = (i / 48 for i in range(20, 79))
            poses = TIGHT_CARRY_POSES
            min_descent = 28
        times = tuple(times)
        for aim_x in aims:
            dx, dy = aim_x - self.x, target.surface(aim_x) - self.y
            for t in times:
                drift = self.planned_gust * max(0, t - GUST_DELAY * STEP) ** 2 / 2
                vx, vy = (dx - drift) / t - self.wind * t / 2, dy / t - GRAVITY * t / 2
                speed = math.hypot(vx, vy)
                push, pull = self._contact_forces(vx, vy)
                if (vy + GRAVITY * t > min_descent and MIN_SPEED <= speed <= MAX_SPEED - 12
                        and push > 0 and abs(pull) <= push * 1.65):
                    score = abs(vy + 473) + abs(aim_x - target.center) * 0.28 + abs(t - 1.04) * 68
                    options.append((score, vx, vy, speed, t))
        for _, vx, vy, speed, t in sorted(options):
            landing_step = self._clear_arc(vx, vy, t, target)
            if landing_step:
                for pose in poses:
                    if self._clear_cup_arc(vx, vy, landing_step, pose):
                        return vx, vy, speed, pose
        return None

    def _contact_forces(self, vx, vy):
        hit = self.brace.hit
        return vx * hit.nx + vy * hit.ny, -vx * hit.ny + vy * hit.nx

    def _release_pose(self):
        hx, hy = self._grip()
        ax, ay = self.brace.hit.point
        return math.atan2(ay - hy, (ax - hx) * self.facing), math.hypot(ax - hx, ay - hy)

    def _clear_cup_arc(self, vx, vy, landing_step, pose):
        angle, length = self._release_pose()
        previous = self.brace.hit.point
        plank = self.effects.get("step", {}).get("ledge")
        ledges = tuple(LEDGES[i] for i in obstacle_indices(
            self.y - vy * vy / (2 * GRAVITY) - GRIP_Y - length - 16, self.y + length + 30))
        for frame in range(1, landing_step + 1):
            t = STEP * frame
            gust_time = STEP * max(0, frame - GUST_DELAY)
            x = self.x + vx * t + self.wind * t * (t + STEP) / 2
            x += self.planned_gust * gust_time * (gust_time + STEP) / 2
            y = self.y + vy * t + GRAVITY * t * (t + STEP) / 2
            tip = carry_tip(self._flying_grip(x, y, t), self.facing, angle, length, pose, t)
            hit = cup_contact(previous, tip, nearby=ledges, extra=plank)
            if hit:
                return False
            previous = tip
        return True

    def _clear_arc(self, vx, vy, t, target):
        # Predict using the same timestep as the live collision solver.
        steps = max(1, math.ceil((t + 0.15) / STEP))
        ox, oy = self.x, self.y
        indices = obstacle_indices(min(self.y - vy * vy / (2 * GRAVITY), target.y) - 32,
                                   max(self.y, target.y) + 135)
        nearby = [LEDGES[i] for i in indices]
        plank = self.effects.get("step", {}).get("ledge")
        surfaces = sorted([*nearby, plank] if plank else nearby, key=lambda item: item.y)
        for i in range(1, steps + 1):
            u = STEP * i
            integral = u * (u + STEP) / 2
            gust_steps = max(0, i - GUST_DELAY)
            gust_time = gust_steps * STEP
            drift = self.planned_gust * gust_time * (gust_time + STEP) / 2
            x = self.x + vx * u + self.wind * integral + drift
            y = self.y + vy * u + GRAVITY * integral
            if x < 22 or x > WIDTH - 22:
                return False
            speed = vy + GRAVITY * u
            for ledge in nearby:
                rim = ledge.surface(clamp(x, ledge.x, ledge.right))
                underside = ledge.underside(rim)
                center = y - 31
                left, right = ledge.x + 7, ledge.right - 7
                if rim + 12 < center < underside and (ox <= left < x or x < right <= ox):
                    return False
                if speed < 0 and center < underside <= oy - 31 and ledge.x + 29 < x < ledge.right - 29:
                    return False
            if speed >= 0:
                horizontal = vx + self.wind * u + self.planned_gust * gust_time
                for ledge in surfaces:
                    left, right = ledge.landing_bounds(horizontal)
                    if (left <= x <= right
                            and oy <= ledge.surface(ox) and y >= ledge.surface(x)):
                        return i if ledge is target else 0
            ox, oy = x, y
        return False

    def _grip(self):
        ox, oy = self._grip_offset()
        return self.x + ox, self.y + oy

    def _grip_offset(self):
        fold = 0.0 if self.on_ledge is not None else carry_blend(self.flight_steps * STEP * 1.75)
        if self.rim_grab is not None:
            fold *= 1 - clamp((self.run_clock - self.rim_grab.start) / 0.54, 0, 1)
        return self.facing * (GRIP_X - 60 * fold), -GRIP_Y + 68 * fold

    def _flying_grip(self, x, y, time):
        fold = carry_blend(time * 1.75)
        return x + self.facing * (GRIP_X - 60 * fold), y - GRIP_Y + 68 * fold

    def _rock_underfoot(self):
        if self.on_ledge is None:
            return None
        if self.on_ledge >= 0:
            return LEDGES[self.on_ledge]
        return self.effects.get("step", {}).get("ledge")

    def _brace_rod(self, ledge):
        if self.brace is None:
            _, destination = self._target()
            self.facing = 1 if destination >= self.x else -1
            if self.run_clock < min(self.auto_at - 0.25, self.landed_at + 2.0):
                self._seek_cup(ledge)
                return
            grip = self._grip()
            distances = (148, 98, 49, 0, -48)
            if self.reposition_attempt:
                offset = (self.reposition_attempt + self.reposition_attempt // 5) % len(distances)
                distances = distances[offset:] + distances[:offset]
            for distance in distances:
                x = clamp(self.x + self.facing * distance, ledge.x + 12, ledge.right - 12)
                hit = cup_contact(grip, (x, ledge.surface(x) + 9), extra=ledge if self.on_ledge < 0 else None)
                if hit and hit.ledge is ledge and hit.ny < -0.7:
                    self.brace = Brace((self.x, self.y), hit, self.run_clock, self.facing)
                    break
            if self.brace is None:
                self._start_reposition()
                return
            climate = CHAPTERS[ledge.chapter]
            phase = math.sin(self.last_ledge * 1.39 + 1)
            weather = self._active_weather()
            self.wind = climate.wind * phase + weather.wind * self.facing
            gust = climate.gust if ledge.exposed else 0
            gust = max(gust, weather.gust) * weather.gust_scale
            self.gust = math.copysign(gust, phase) if gust else 0
        brace = self.brace
        blend = clamp((self.run_clock - brace.held_from) / max(0.25, self.auto_at - brace.held_from), 0, 1)
        blend = blend * blend * (3 - 2 * blend)
        # The suction cup stays fixed while the body pivots around the rock.
        px, py = brace.body
        ax, ay = brace.hit.point
        dx = px + brace.facing * GRIP_X - ax
        dy = py - GRIP_Y - ay
        angle = 0.48 * blend * brace.facing
        c, s = math.cos(angle), math.sin(angle)
        self.x = ax + dx * c - dy * s - brace.facing * GRIP_X
        self.y = ay + dx * s + dy * c + GRIP_Y

    def _seeking_tip(self):
        hx, hy = self._grip()
        seek = clamp((self.run_clock - self.landed_at) / 2, 0, 1)
        angle = -0.95 + 2.52 * seek + 0.45 * math.sin(seek * math.pi)
        return hx + self.facing * 134 * math.cos(angle), hy + 134 * math.sin(angle)

    def _seek_cup(self, ledge):
        desired = self._seeking_tip()
        hit = cup_contact(self.cup_at or self._grip(), desired, extra=ledge if self.on_ledge < 0 else None)
        self.cup_at = hit.point if hit else desired
        self.cup_block = hit

    def _rod(self):
        hx, hy = self._grip()
        if self.rim_grab is not None:
            return hx, hy, *self.rim_grab.hit.point, True
        if self.brace is not None:
            return hx, hy, *self.brace.hit.point, True
        if self.cup_at is not None:
            return hx, hy, *self.cup_at, self.cup_block is not None
        return hx, hy, *self._seeking_tip(), False

    def _record_rod_hit(self, hit):
        self.last_impact = self.run_clock, hit
        self.rod_hits += 1

    def _air_rod(self):
        grip = self._grip()
        desired = carry_tip(grip, self.facing, self.release_angle, self.rod_length, self.carry_pose,
                            self.flight_steps * STEP)
        start = self.cup_at or grip
        plank = self.effects.get("step", {}).get("ledge")
        hit = cup_contact(start, desired, extra=plank)
        self.cup_at = hit.point if hit else desired
        self.cup_block = hit
        if hit is None or self.run_clock < self.cup_cooldown:
            return
        self.cup_cooldown = self.run_clock + 0.15
        arm_speed = -((desired[0] - start[0]) * hit.nx + (desired[1] - start[1]) * hit.ny) / STEP
        incoming = max(0, -self.vx * hit.nx - self.vy * hit.ny)
        impulse = min(166, max(0, arm_speed) * 0.20 + incoming * 0.36)
        self.vx += hit.nx * impulse
        self.vy += hit.ny * impulse
        self.replan = True
        self._record_rod_hit(hit)

    def _steer(self):
        target = self.flight_target
        if not self.replan or target is None or not self.jets or self.run_clock < self.boost_at:
            return
        delta_x = target.center - self.x
        if abs(delta_x) < 95 and self.vy > 40 and 0 < self.y - target.y < 70:
            self._boost(*direction(delta_x, target.y - self.y - 18))
            return
        # Re-evaluate the actual trajectory after collisions and net rebounds.
        altitude = self.y - target.surface(clamp(self.x, target.x, target.right))
        discriminant = self.vy * self.vy - 2 * GRAVITY * altitude
        if discriminant < 0:
            return
        t = (-self.vy + math.sqrt(discriminant)) / GRAVITY
        gust_time = max(0, t - max(0, GUST_DELAY - self.flight_steps) * STEP)
        gust = self.gust + self.gift_wind
        predicted = self.x + self.vx * t + (self.wind * t * t + gust * gust_time * gust_time) / 2
        left, right = target.landing_bounds(self.vx + self.wind * t + gust * gust_time)
        if t > 0.22 and not left <= predicted <= right:
            error = target.center - predicted
            self._boost(*direction(error, -28), impulse=clamp(abs(error) / t, 42, 150))

    def _substep(self):
        self.run_clock += STEP
        for name, effect in list(self.effects.items()):
            if effect.get("until", math.inf) <= self.run_clock:
                self.effects.pop(name)
        for votes in self.votes.values():
            for uid, at in list(votes.items()):
                if self.run_clock - at >= 12:
                    del votes[uid]
        if self.recover_at:
            if self.run_clock >= self.recover_at:
                ledge = LEDGES[self.recover_to]
                self.x = ledge.center if self.recover_to else START_X
                self._land(self.recover_to, ledge)
                if self.boat_riding:
                    self.camera_y = min(0.0, self.y - 548)
                self.recover_at = 0.0
                self.boat_riding = False
        elif not self.finished:
            if (self.on_ledge is not None and self.run_clock >= self.weather_until_clock
                    and (self.brace is None or self.reposition_to is not None)):
                self._expire_weather()
            if self.on_ledge is not None:
                ledge = self._rock_underfoot()
                if ledge is None:
                    self.on_ledge = None
                    self.brace = None
                elif self.reposition_to is not None:
                    self._reposition(ledge)
                else:
                    self._brace_rod(ledge)
                    self._auto()
            if self.rim_grab is not None:
                self._pull_up()
            elif self.on_ledge is None:
                if (self.flight_since and self.run_clock - self.flight_since > 6
                        and math.hypot(self.vx, self.vy) < 90):
                    self._break_free()
                if not self.recover_at:
                    self._steer()
                    if self.on_ledge is None and self.rim_grab is None:
                        self._flight()
        target_x = clamp(self.x - 430, -90, 142)
        target_y = min(0.0, self.y - 548)
        if self.recover_at:
            target_y = min(0, self.y - 590)
        self.camera_x += (target_x - self.camera_x) * 0.016
        self.camera_y += (target_y - self.camera_y) * 0.026
        self.camera_y = max(self.camera_y, min(0.0, self.y - (HEIGHT - 52)))

    def _fall_catch(self):
        if (not self.magnet_flight or self.rim_grab is not None
                or self.vy < 40 or not self.replan):
            return False
        grip = self._grip()
        start = self.cup_at or grip
        for index in ledge_indices(self.y - 40, self.y + 120):
            ledge = LEDGES[index]
            for edge in (ledge.x + 5, ledge.right - 5):
                rim = (edge, ledge.surface(edge) + 6)
                if rim[1] - grip[1] < 20 or math.dist(grip, rim) > FALL_REACH:
                    continue
                hit = cup_contact(start, rim, nearby=(ledge,))
                if hit and hit.ledge is ledge and hit.y <= ledge.surface(edge) + 17:
                    self._hold_rim(index, hit)
                    self._notice("磁力吸盘吸住了下层岩沿")
                    return True
        return False

    def _break_free(self):
        self.stuck_tries += 1
        self.flight_since = self.run_clock
        if self.stuck_tries > 3:
            self._notice("半空脱困失败 · 落水重爬")
            self._fall()
            return
        hit = self.last_impact[1] if self.last_impact and self.run_clock - self.last_impact[0] < 1.2 else None
        if hit is not None:
            self.vx += hit.nx * 520
            self.vy += hit.ny * 520
        else:
            side = 1 if self.flight_target is not None and self.flight_target.center >= self.x else -1
            self.vx += side * 260
            self.vy -= 260
        self.jets = max(self.jets, 1)
        self.replan = True
        self.boost_at = self.run_clock
        self._notice("半空卡住 · 撑杆脱困")

    def _flight(self):
        before_x, before_y = self.x, self.y
        self.flight_steps += 1
        gust_now = (self.gust + self.gift_wind) if self.flight_steps > GUST_DELAY else 0
        if self.flight_steps == GUST_DELAY + 1:
            if self.gust:
                self.seen_gusts.add(self.last_ledge)
            if self.gift_wind:
                by = self.effects.pop("squall")["by"]
                self.replan = True
                self._notice(f"{by} 的逆风吹来了")
                self._cue("观众的逆风礼物刚把半空中的角色推离落点，需要向反方向修正", "惊讶")
            elif self.gust and not self.planned_gust:
                self.replan = True
                if abs(gust_now) >= 155:
                    self._notice("阵风从侧面推过来了")
                    self._cue("半空突然遇到山风，落点偏离，需要用杆和喷气修正", "惊讶")
        self.vy += GRAVITY * STEP
        if self.run_clock < self.nitro_until:
            self.vy = max(-NITRO_RISE, self.vy - NITRO_LIFT * STEP)
        if self.run_clock < self.rocket_until:
            self.vy = max(-ROCKET_RISE, self.vy - ROCKET_LIFT * STEP)
        self.vx += (self.wind + gust_now) * STEP
        self.x += self.vx * STEP
        if self.x < 22 or self.x > WIDTH - 22:
            self.x = clamp(self.x, 22, WIDTH - 22)
            self.vx *= -0.45
            self.replan = True
        self.y += self.vy * STEP
        self._rock_impact(before_x, before_y)
        self._air_rod()
        if self._fall_catch():
            return
        self.best_y = min(self.best_y, self.y)
        self.deepest_fall = max(self.deepest_fall, height(self.best_y) - height(self.y))
        if self.vy >= 0:
            surfaces = [(i, LEDGES[i]) for i in ledge_indices(before_y - 20, self.y + 21)]
            plank = self.effects.get("step")
            if plank and "ledge" in plank:
                surfaces.append((-1, plank["ledge"]))
            net = self.effects.get("net")
            vulnerable = self.flight_target is None or self.replan or self.y > self.flight_target.y + 40
            if net and vulnerable and before_y <= net["y"] <= self.y and abs(self.x - net["x"]) <= 154:
                self.y = net["y"] - 1
                self.vy, self.vx = -360, self.vx * 0.42
                self.replan = True
                self.effects.pop("net")
                self._notice("弹幕的救生网接住了大肥鱼")
                self._cue("快跌下去时被观众的救生网接住并弹回半空", "惊喜")
                return
            for index, ledge in sorted(surfaces, key=lambda item: item[1].y):
                left, right = ledge.landing_bounds(self.vx)
                if (left <= self.x <= right
                        and before_y <= ledge.surface(before_x) and self.y >= ledge.surface(self.x)):
                    self._land(index, ledge)
                    if index == -1:
                        plank["used"] = True
                        plank["until"] = self.run_clock + 10
                        self._notice(f"{plank['by']} 的泡泡踏板接住了大肥鱼")
                        self._cue("观众的泡泡踏板刚接住角色，接下来能从踏板借力", "开心")
                    return
        if self.y >= WATER_Y:
            self._fall()

    def _rock_impact(self, before_x, before_y):
        # Keep the basin solid against the front rim and underside of the rock.
        basin_top = self.y - 31
        for index in obstacle_indices(min(basin_top, before_y - 31), max(basin_top, before_y - 31) + 20):
            ledge = LEDGES[index]
            rim = ledge.surface(clamp(self.x, ledge.x, ledge.right))
            underside = ledge.underside(rim)
            if rim + 12 < basin_top < underside:
                left, right = ledge.x + 7, ledge.right - 7
                if before_x <= left < self.x:
                    self.x, self.vx = left - 2, -abs(self.vx) * 0.47
                    self.replan = True
                elif self.x < right <= before_x:
                    self.x, self.vx = right + 2, abs(self.vx) * 0.47
                    self.replan = True
            if (self.vy < 0 and basin_top < underside <= before_y - 31
                    and ledge.x + 29 < self.x < ledge.right - 29):
                self.y, self.vy = underside + 31, abs(self.vy) * 0.38
                self.replan = True
                break

    def advance(self, delta):
        with self.lock:
            if not self.auto:
                return
            elapsed = clamp(delta, 0, 0.15)
            if self.finished:
                self.result_left = max(0, self.result_left - elapsed)
                if self.result_left == 0:
                    self.reset(next_round=True)
                return
            self.accumulator += elapsed
            while self.accumulator >= STEP and not self.finished:
                self.accumulator -= STEP
                self._substep()

    def on_event(self, event):
        with self.lock:
            if event.get("type") == "danmaku" and not self.finished:
                return self._vote(event)
            if event.get("type") == "gift" and event.get("paid"):
                return self._gift(event)
            if event.get("type") == "follow" and not self.finished:
                return self._follow(event)
            return False

    def _vote(self, event):
        kind = COMMANDS.get(str(event.get("text") or "").strip())
        uid = str(event.get("uid") or (event.get("name") if event.get("test") else ""))
        if not kind or not uid or kind in self.effects or self.cooldowns.get(kind, 0) > self.run_clock:
            return False
        votes = self.votes[kind]
        votes.setdefault(uid, self.run_clock)
        if len(votes) < self.settings().get(kind, DEFAULTS[kind]):
            return True
        votes.clear()
        self.cooldowns[kind] = self.run_clock + 15
        if kind == "guidance":
            index, x = self._target()
            self.effects[kind] = {"until": self.run_clock + 8, "x": x, "y": LEDGES[index].surface(x)}
            self._notice("弹幕给下一块落脚石点了灯")
            self._cue("观众的指路弹幕刚给下一块石头点亮了灯", "开心")
        elif kind == "air":
            self.effects[kind] = {"until": self.run_clock + 30}
            self._notice("备用气已攒好 · 下一跳可额外喷气")
            self._cue("观众用弹幕帮忙攒了一次备用喷气", "开心")
        else:
            # Deploy under the current crossing so the net can catch an active fall.
            x = clamp(self.x + self.vx * 0.16, 115, WIDTH - 115)
            self.effects[kind] = {"until": self.run_clock + 15, "x": x, "y": self.y + 95}
            self._notice("弹幕把救生网张开了")
        return True

    def _gift(self, event):
        settings = self.settings()
        gift = str(event.get("gift") or "").strip()
        kind = next((key for key in GIFTS if gift and gift == settings.get(key + "_gift", "").strip()), None)
        return self._arm_gift(kind, event) if kind else False

    def _arm_gift(self, kind, event):
        identity = event.get("gift_tid") or event.get("gift_rnd")
        if identity:
            transaction = (str(event.get("room_id") or ""), str(identity))
            if transaction in self.gift_ids:
                return False
            if len(self.gift_ids) >= 2048:
                self.gift_ids.pop(next(iter(self.gift_ids)))
            self.gift_ids[transaction] = None
        by = " ".join(str(event.get("name") or "观众").split())[:16]
        if kind == "rocket":
            return self._rocket(by)
        if kind in self.effects:
            return False
        self.effects[kind] = {"by": by}
        if kind == "school":
            self.effects[kind]["jumps"] = 3
        elif kind == "magnet":
            self.effects[kind]["jumps"] = MAGNET_JUMPS
            self._notice(f"{by} 的磁力吸盘启动 · 接下来 {MAGNET_JUMPS} 跳防滑自动抓边")
            self._cue(f"观众的磁力吸盘正在增强吸附力，接下来{MAGNET_JUMPS}跳不会打滑，失手也能自动钩住岩沿", "开心")
            return True
        elif kind == "feather":
            self._notice(f"{by} 的金羽毛待命 · 摔落后拉回最高点")
            self._cue(f"观众的{by}备好了金羽毛，摔落时能把角色拉回最高岩沿", "开心")
            return True
        if kind in SABOTAGE_GIFTS:
            self._notice(f"{by} 的捣乱待命 · {GIFTS[kind]}")
        else:
            self._notice(f"{by} 派出了{GIFTS[kind]}")
        return True

    def _rocket(self, by):
        if self.finished or self.recover_at or self.rocket_until > self.run_clock:
            return False
        self.rocket_until = self.run_clock + ROCKET_BURN
        if self.on_ledge is not None:
            index, _ = self._target()
            target = LEDGES[min(len(LEDGES) - 1, index + 1)]
            self.vx = clamp((target.center - self.x) / 2.2, -260, 260)
            self.vy = -470
            if self.brace is not None:
                self.release_angle, self.rod_length = self._release_pose()
                self.cup_at = self.brace.hit.point
            self.cup_block = None
            self.cup_cooldown = self.run_clock
            self.brace = None
            self.rim_grab = None
            self.on_ledge = None
            self.flight_steps = 0
            self.flight_since = self.run_clock
            self.stuck_tries = 0
            self.charge_at = None
            self.auto_plan = None
            self.flight_target = None
            self.reposition_to = None
            self.jets = 0
            self.replan = False
            self.magnet_flight = False
            self.turns += 1
        elif self.rim_grab is not None:
            self.rim_grab = None
            self.cup_block = None
            self.cup_cooldown = self.run_clock
            self.vy = min(self.vy, -470)
        self._notice(f"{by} 的火箭点火 · 强力上升")
        self._cue("观众送出的火箭点火，大肥鱼被推着往上连窜好几层", "惊喜")
        return True

    def _follow(self, event):
        now = time.monotonic()
        uid = str(event.get("uid") or (event.get("name") if event.get("test") else ""))
        if not uid or self.follow_ids.get(uid, 0) > now:
            return False
        if len(self.follow_ids) >= 2048:
            self.follow_ids.pop(next(iter(self.follow_ids)))
        self.follow_ids[uid] = now + FOLLOW_COOLDOWN
        return self._nitro(event)

    def _nitro(self, event):
        if self.finished or self.recover_at or self.nitro_until > self.run_clock:
            return False
        by = " ".join(str(event.get("name") or "观众").split())[:16]
        self.nitro_until = self.run_clock + NITRO_BURN
        if self.on_ledge is not None:
            index, _ = self._target()
            target = LEDGES[min(len(LEDGES) - 1, index + 1)]
            self.vx = clamp((target.center - self.x) / 2.0, -240, 240)
            self.vy = -380
            if self.brace is not None:
                self.release_angle, self.rod_length = self._release_pose()
                self.cup_at = self.brace.hit.point
            self.cup_block = None
            self.cup_cooldown = self.run_clock
            self.brace = None
            self.rim_grab = None
            self.on_ledge = None
            self.flight_steps = 0
            self.flight_since = self.run_clock
            self.stuck_tries = 0
            self.charge_at = None
            self.auto_plan = None
            self.flight_target = None
            self.reposition_to = None
            self.jets = 0
            self.replan = False
            self.turns += 1
        elif self.rim_grab is not None:
            self.rim_grab = None
            self.cup_block = None
            self.cup_cooldown = self.run_clock
            self.vy = min(self.vy, -380)
        self._notice(f"{by} 关注了 · 氮气加速")
        self._cue(f"新观众{by}刚关注，氮气推进器点火，正在向上猛冲", "惊喜")
        return True

    def simulate(self, label, viewer):
        with self.lock:
            if self.finished:
                return False
            if label in COMMANDS:
                return self._vote({"text": label, "uid": f"sim-{viewer}"})
            if label in GIFTS:
                return self._arm_gift(label, {"name": "试播观众"})
            if label in FOLLOW_EFFECTS:
                return self._follow({"name": "试播观众", "uid": f"sim-follow-{viewer}", "test": True})
            return False

    def snapshot(self):
        with self.lock:
            now = self.run_clock
            settings = self.settings()
            weather = self._active_weather()
            chapter = LEDGES[self.last_ledge].chapter if self.on_ledge is not None else chapter_at(self.y)
            visible_ledge = LEDGES[self.last_ledge]
            if self.recover_at or visible_ledge.chapter != chapter:
                visible_ledge = LEDGES[CHECKPOINTS[chapter]]
            hit = (self.rim_grab.hit if self.rim_grab else self.brace.hit if self.brace else self.cup_block)
            recent_impact = self.last_impact if self.last_impact and now - self.last_impact[0] < 0.34 else None
            return {
                "x": self.x, "y": self.y, "vx": self.vx, "vy": self.vy,
                "camera": (self.camera_x, self.camera_y), "facing": self.facing,
                "zone": CHAPTERS[chapter].name, "standing": self.on_ledge,
                "chapter": chapter, "rod": self._rod(), "wind": self.wind,
                "rod_normal": (hit.nx, hit.ny) if hit else None, "rod_impact": recent_impact,
                "rod_hits": self.rod_hits, "crouch": (GRIP_Y + self._grip_offset()[1]) / 68,
                "gust": self.gust + self.gift_wind if self.on_ledge is None and self.flight_steps > GUST_DELAY else 0,
                "height": height(self.y), "best": height(self.best_y), "fall": self.deepest_fall,
                "record": self.record_ledge, "turns": self.turns, "clock": now, "misses": self.misses,
                "slips": self.slips, "comebacks": self.comebacks, "terrain": visible_ledge.surface_name,
                "landmark": visible_ledge.landmark,
                "weather": {"kind": weather.key, "name": weather.name, "hint": weather.hint,
                            "since": self.weather_since, "enabled": self.weather_enabled(),
                            "prev": self.weather_prev.key if self.weather_enabled() else "clear",
                            "mix": (clamp((now - self.weather_since) / WEATHER_BLEND, 0, 1)
                                    if self.weather_enabled() and weather.key != self.weather_prev.key
                                    else 1)},
                "power": self._power(), "charging": self.charge_at is not None, "jets": self.jets,
                "nitro": max(0.0, self.nitro_until - now),
                "rocket": max(0.0, self.rocket_until - now),
                "auto": self.auto, "finished": self.finished, "round": self.round_number,
                "result_left": math.ceil(self.result_left), "recovering": bool(self.recover_at),
                "recovery_left": max(0, self.recover_at - now), "boat_riding": self.boat_riding,
                "effects": {key: value.copy() for key, value in self.effects.items()},
                "votes": {label: (len(self.votes[key]), settings.get(key, DEFAULTS[key]))
                          for label, key in COMMANDS.items()},
                "gifts": {key: settings.get(key + "_gift", "").strip() for key in GIFTS},
                "notice": next((line for expire, line in reversed(self.news) if expire > now), ""),
            }
