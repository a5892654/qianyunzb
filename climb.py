"""Physics and audience interaction for 大肥鱼爬出海."""

from collections import deque
from dataclasses import dataclass
import math
from threading import RLock

from climb_contact import Contact, carry_blend, carry_tip, cup_contact
from climb_course import BASE_Y, CHAPTERS, CHECKPOINTS, LEDGES, Ledge, ledge_indices


TITLE = "大肥鱼爬出海"
WIDTH, HEIGHT = 1280, 720
GRAVITY = 680
STEP = 1 / 120
GUST_DELAY = 43
ROUND_BREAK = 7.0
MIN_SPEED, MAX_SPEED = 250, 675
BODY_RADIUS = 26
BRACE_TIME = 4.89
GRIP_X, GRIP_Y = 66, 109
CARRY_POSES = ((-0.46, 0.78), (-1.52, 0.74), (-2.65, 0.66), (2.28, 0.64), (1.03, 0.57),
               (0.12, 0.33), (1.10, 0.33), (-1.52, 0.33), (2.90, 0.33), (2.95, 0.10))
GIFTS = {"boat": "打捞艇", "step": "泡泡踏板", "school": "鱼群跟拍"}
DEFAULTS = {"guidance": 1, "air": 2, "net": 3,
            "boat_gift": "", "step_gift": "", "school_gift": "", "narrate": True}


COMMANDS = {"指路": "guidance", "打气": "air", "接住": "net"}


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
        self.round_number = 1
        self.auto = True
        self.effects = {}
        self.reset()

    def reset(self, *, next_round=False):
        with self.lock:
            self.round_number = self.round_number + 1 if next_round else 1
            pending = {}
            for gift in GIFTS:
                effect = self.effects.get(gift)
                if not effect or effect.get("used") or effect.get("jumps", 1) <= 0:
                    continue
                if gift == "step":
                    pending[gift] = {"by": effect["by"]}
                else:
                    pending[gift] = effect.copy()
            self.run_clock = 0.0
            self.accumulator = 0.0
            self.x, self.y = 430.0, float(BASE_Y)
            self.vx = self.vy = 0.0
            self.facing = 1
            self.camera_x = self.camera_y = 0.0
            self.on_ledge = self.last_ledge = self.record_ledge = self.checkpoint = 0
            self.best_y = self.y
            self.deepest_fall = self.turns = 0
            self.charge_at = None
            self.jets = 0
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
            self.flight_steps = 0
            self.seen_gusts = set()
            self.misses = 0
            self.auto_plan = None
            self.flight_target = None
            self.replan = False
            self.reposition_to = None
            self.reposition_attempt = 0
            self.boost_at = 0.0
            self.recover_at = 0.0
            self.recover_to = 0
            self.boat_riding = False
            self.tethered = False
            self.effects = pending
            self.votes = {key: {} for key in COMMANDS.values()}
            self.cooldowns = {}
            self.news = deque(maxlen=3)
            self.voice = deque(maxlen=4)

    def set_auto(self, enabled):
        with self.lock:
            self.auto = bool(enabled)

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
        self.charge_at = None
        self.jets = 1 + int(self.effects.pop("air", None) is not None)
        self.boost_at = self.run_clock + 0.36
        self.replan = False
        self.turns += 1
        if self.effects.get("school", {}).get("jumps", 0) == 0:
            self.effects.pop("school", None)
        if "school" in self.effects:
            self.effects["school"]["jumps"] -= 1

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
        self.x = clamp(self.x, ledge.x + BODY_RADIUS, ledge.right - BODY_RADIUS)
        self.y = ledge.surface(self.x)
        self.vx = self.vy = 0.0
        self.on_ledge = index
        self.landed_at = self.run_clock
        hold = 3.5 if index < 0 else BRACE_TIME + (0.28 if ledge.material in ("snow", "steel") else 0)
        self.auto_at = self.run_clock + hold
        self.brace = None
        self.rim_grab = None
        self.cup_at = None
        self.cup_block = None
        self.auto_plan = None
        self.flight_target = None
        self.reposition_to = None
        self.reposition_attempt = 0
        if index < 0:
            return
        self.last_ledge = index
        if index > self.record_ledge:
            self.record_ledge = index
            checkpoint = CHECKPOINTS[ledge.chapter]
            if checkpoint > self.checkpoint:
                self.checkpoint = checkpoint
                self._say(f"到{LEDGES[checkpoint].section}了。刚才那几个借力点可真滑。", "开心")
            if index == len(LEDGES) - 1:
                self.finished = True
                self.result_left = ROUND_BREAK
                self._say("终于爬出海了！这次真的到顶了。", "开心")

    def _say(self, line, emotion):
        self.voice.append((line, emotion))

    def _notice(self, line):
        self.news.append((self.run_clock + 6, line))

    def take_voice(self):
        with self.lock:
            voice = self.voice[-1] if self.voice else None
            self.voice.clear()
            return voice

    def _fall(self):
        self.recover_at = self.run_clock + 1.45
        self.recover_to = self._catch_floor()
        boat = self.effects.pop("boat", None)
        self.boat_riding = bool(boat)
        self.tethered = not boat and self.recover_to > self.checkpoint
        if boat:
            self.recover_at += 0.85
            self.recover_to = self.last_ledge
            self._notice(f"{boat['by']} 的打捞艇出发了")
            self._say("船来啦，先把我捞起来！", "惊喜")
        elif self.tethered:
            self._notice("安全绳拽住了 · 回到下方挂点")
            self._say("差点又得从营地重来。", "惊讶")
        else:
            self._notice("溅起水花 · 从最近的营地再出发")
            self._say("我刚刚那一跳，主要是测试水深。", "尴尬")

    def _catch_floor(self):
        chapter = LEDGES[self.last_ledge].chapter
        span = 3 + chapter // 2
        return max(self.checkpoint, self.last_ledge - span)

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
            self.planned_gust = (self.gust if self.last_ledge in self.seen_gusts
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
        for aim_x in (landing_x, target.center, left + 4, right - 4):
            dx, dy = aim_x - self.x, target.surface(aim_x) - self.y
            for i in range(18, 52):
                t = i / 32
                drift = self.planned_gust * max(0, t - GUST_DELAY * STEP) ** 2 / 2
                vx, vy = (dx - drift) / t - self.wind * t / 2, dy / t - GRAVITY * t / 2
                speed = math.hypot(vx, vy)
                push, pull = self._contact_forces(vx, vy)
                if (vy + GRAVITY * t > 54 and MIN_SPEED <= speed <= MAX_SPEED - 12
                        and push > 0 and abs(pull) <= push * 1.65):
                    score = abs(vy + 473) + abs(aim_x - target.center) * 0.28 + abs(t - 1.04) * 68
                    options.append((score, vx, vy, speed, t))
        for _, vx, vy, speed, t in sorted(options):
            landing_step = self._clear_arc(vx, vy, t, target)
            if landing_step:
                for pose in CARRY_POSES:
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
        ledges = tuple(LEDGES[i] for i in ledge_indices(
            self.y - vy * vy / (2 * GRAVITY) - GRIP_Y - length - 148, self.y + length + 30))
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
        indices = tuple(ledge_indices(min(self.y - vy * vy / (2 * GRAVITY), target.y) - 153,
                                      max(self.y, target.y) + 135))
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
            for distance in (148, 98, 49, 0):
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
            self.wind = climate.wind * phase
            self.gust = math.copysign(climate.gust, phase) if ledge.exposed else 0
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
        predicted = self.x + self.vx * t + (self.wind * t * t + self.gust * gust_time * gust_time) / 2
        left, right = target.landing_bounds(self.vx + self.wind * t + self.gust * gust_time)
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
                self.x = ledge.center
                self._land(self.recover_to, ledge)
                self.recover_at = 0.0
                self.boat_riding = False
                self.tethered = False
        elif not self.finished:
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
                self._steer()
                if self.on_ledge is None and self.rim_grab is None:
                    self._flight()
        target_x = clamp(self.x - 430, -90, 142)
        target_y = min(0.0, self.y - 548)
        if self.recover_at:
            target_y = min(0, self.y - 590)
        self.camera_x += (target_x - self.camera_x) * 0.016
        self.camera_y += (target_y - self.camera_y) * 0.026

    def _flight(self):
        before_x, before_y = self.x, self.y
        self.flight_steps += 1
        gust_now = self.gust if self.flight_steps > GUST_DELAY else 0
        if gust_now and self.flight_steps == GUST_DELAY + 1:
            self.seen_gusts.add(self.last_ledge)
            if not self.planned_gust:
                self.replan = True
                if abs(gust_now) >= 155:
                    self._notice("阵风从侧面推过来了")
        self.vy += GRAVITY * STEP
        self.vx += (self.wind + gust_now) * STEP
        self.x += self.vx * STEP
        if self.x < 22 or self.x > WIDTH - 22:
            self.x = clamp(self.x, 22, WIDTH - 22)
            self.vx *= -0.45
            self.replan = True
        self.y += self.vy * STEP
        self._rock_impact(before_x, before_y)
        self._air_rod()
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
                    return
        floor = self._catch_floor()
        if self.y > LEDGES[floor].y + (185 if floor == self.checkpoint else 120):
            self._fall()

    def _rock_impact(self, before_x, before_y):
        # Keep the basin solid against the front rim and underside of the rock.
        basin_top = self.y - 31
        for index in ledge_indices(basin_top - 140, basin_top + 20):
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
        elif kind == "air":
            self.effects[kind] = {"until": self.run_clock + 30}
            self._notice("备用气已攒好 · 下一跳可额外喷气")
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
        if kind in self.effects:
            return False
        by = " ".join(str(event.get("name") or "观众").split())[:16]
        self.effects[kind] = {"by": by}
        if kind == "school":
            self.effects[kind]["jumps"] = 3
        self._notice(f"{by} 派出了{GIFTS[kind]}")
        return True

    def simulate(self, label, viewer):
        with self.lock:
            if self.finished:
                return False
            if label in COMMANDS:
                return self._vote({"text": label, "uid": f"sim-{viewer}"})
            if label in GIFTS:
                return self._arm_gift(label, {"name": "试播观众"})
            return False

    def snapshot(self):
        with self.lock:
            now = self.run_clock
            settings = self.settings()
            hit = (self.rim_grab.hit if self.rim_grab else self.brace.hit if self.brace else self.cup_block)
            recent_impact = self.last_impact if self.last_impact and now - self.last_impact[0] < 0.34 else None
            return {
                "x": self.x, "y": self.y, "vx": self.vx, "vy": self.vy,
                "camera": (self.camera_x, self.camera_y), "facing": self.facing,
                "zone": LEDGES[self.checkpoint].section, "standing": self.on_ledge,
                "chapter": LEDGES[self.checkpoint].chapter, "rod": self._rod(), "wind": self.wind,
                "rod_normal": (hit.nx, hit.ny) if hit else None, "rod_impact": recent_impact,
                "rod_hits": self.rod_hits, "crouch": (GRIP_Y + self._grip_offset()[1]) / 68,
                "gust": self.gust if self.on_ledge is None and self.flight_steps > GUST_DELAY else 0,
                "height": height(self.y), "best": height(self.best_y), "fall": self.deepest_fall,
                "record": self.record_ledge, "turns": self.turns, "clock": now, "misses": self.misses,
                "power": self._power(), "charging": self.charge_at is not None, "jets": self.jets,
                "auto": self.auto, "finished": self.finished, "round": self.round_number,
                "result_left": math.ceil(self.result_left), "recovering": bool(self.recover_at),
                "recovery_left": max(0, self.recover_at - now), "boat_riding": self.boat_riding,
                "tether": LEDGES[self.recover_to] if self.tethered else None,
                "effects": {key: value.copy() for key, value in self.effects.items()},
                "votes": {label: (len(self.votes[key]), settings.get(key, DEFAULTS[key]))
                          for label, key in COMMANDS.items()},
                "gifts": {key: settings.get(key + "_gift", "").strip() for key in GIFTS},
                "notice": next((line for expire, line in reversed(self.news) if expire > now), ""),
            }
