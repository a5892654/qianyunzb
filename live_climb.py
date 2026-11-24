"""The cliff scene shared by the operator page and the native broadcast source."""

from functools import lru_cache
import math
from pathlib import Path
from threading import Lock

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from climb import (FOLLOW_EFFECTS, HEIGHT, HELP_GIFTS, LEDGES, NITRO_BURN, POWER_GIFTS,
                   ROCKET_BURN, SABOTAGE_GIFTS, TITLE, WIDTH, clamp)
from climb_course import CHAPTERS, CHECKPOINTS, LANDMARKS, WATER_Y, ledge_indices
from climb_landmarks import architecture_faces, ledge_faces
from live_capture import OUT_H, OUT_W


PALE = (247, 238, 214)
SOFT = (220, 217, 203)
GOLD = (224, 188, 112)
SAGE = (169, 212, 186)
CORAL = (247, 166, 143)
ICE = (157, 205, 231)
INK = (48, 47, 43)


@lru_cache(maxsize=12)
def _font(size):
    for name in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _text(draw, pos, text, size, color=PALE, anchor="lt"):
    draw.text(pos, text, font=_font(size), fill=color, anchor=anchor,
              stroke_width=2, stroke_fill=(37, 39, 36))


def _paste(frame, image, x, y):
    frame.paste(image, (round(x), round(y)), image)


@lru_cache(maxsize=64)
def _gift_name_lines(name):
    font = _font(16)
    lines = [""]
    for char in " ".join(name.split()):
        if font.getlength(lines[-1] + char) > 174:
            lines.append("")
        lines[-1] += char
    return tuple(lines)


@lru_cache(maxsize=8)
def _landmark_bitmap(landmark):
    faces = architecture_faces(landmark)
    xs, ys = zip(*(point for _, polygon in faces for point in polygon))
    x, y = math.floor(min(xs)), math.floor(min(ys))
    layer = Image.new("RGBA", (math.ceil(max(xs)) - x + 1, math.ceil(max(ys)) - y + 1))
    draw = ImageDraw.Draw(layer)
    for color, polygon in faces:
        draw.polygon([(px - x, py - y) for px, py in polygon], fill=(*color, 255))
    pixels = np.array(layer)
    width, height = layer.size
    random = np.random.default_rng(landmark.first)
    grain = random.normal(0, 2.8, (height, width))
    light = np.linspace(10, -10, width)[None, :] + np.linspace(5, -8, height)[:, None]
    pixels[..., :3] = np.clip(pixels[..., :3].astype(np.float32) + (light + grain)[..., None], 0, 255)
    return Image.fromarray(pixels), x, y


class ClimbScene:
    def __init__(self, game, web_dir):
        self.game = game
        self.assets = Path(web_dir) / "assets" / "climb"
        self._render_lock = Lock()
        self._ready = False

    def _load(self):
        self.background = Image.open(self.assets / "landscape.webp").convert("RGB").resize((1440, 810))
        terrain = self.assets / "terrain"
        self.backdrops = [
            ImageOps.fit(Image.open(terrain / f"background-{number}.webp").convert("RGB"),
                         (1440, 810), Image.Resampling.LANCZOS, centering=(0.5, 0.49))
            for number in range(6)
        ]
        types = {ledge.material for ledge in LEDGES}
        self.material = {name: Image.open(terrain / f"{name}.webp").convert("RGBA") for name in types}
        self.recesses = {
            name: ImageEnhance.Brightness(value).enhance(0.46).resize((220, 630), Image.Resampling.LANCZOS)
            for name, value in self.material.items() if name not in {"steel", "wood", "scaffold"}
        }
        original = Image.open(self.assets / "rig-body.webp").convert("RGBA")
        original.thumbnail((225, 202), Image.Resampling.LANCZOS)
        self.players = {1: original, -1: ImageOps.mirror(original)}
        self._ready = True

    def render(self):
        state = self.game.snapshot()
        with self._render_lock:
            if not self._ready:
                self._load()
            return self._compose(state)

    def frame(self):
        bgr = cv2.cvtColor(np.asarray(self.render()), cv2.COLOR_RGB2BGRA)
        return cv2.resize(bgr, (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR)

    def jpeg(self):
        bgr = cv2.cvtColor(np.asarray(self.render()), cv2.COLOR_RGB2BGR)
        ok, data = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 86])
        return data.tobytes() if ok else b""

    @lru_cache(maxsize=128)
    def _ledge_bitmap(self, material, width, depth, slope):
        texture = self.material[material]
        rendered = texture.resize((width + 12, depth + 20), Image.Resampling.LANCZOS)
        pad = abs(slope) // 2 + 3
        if slope:
            rendered = rendered.transform((rendered.width, rendered.height + 2 * pad), Image.Transform.AFFINE,
                                          (1, 0, 0, -slope / rendered.width, 1, slope / 2 - pad),
                                          Image.Resampling.BICUBIC)
        ledge_top = clamp(depth * (0.11 if material in {"wood", "steel", "scaffold"} else 0.09), 4, 28)
        return rendered, ledge_top + (pad if slope else 0)

    def _compose(self, state):
        cx, cy = state["camera"]
        offset = round(clamp(78 + cx * 0.23, 0, 160))
        down = round(clamp(75 + 16 * math.sin(cy / 680), 0, 90))
        chapter = CHAPTERS[state["chapter"]]
        background = self.background if state["height"] < 40 and state["chapter"] == 0 else self.backdrops[chapter.backdrop]
        frame = background.crop((offset, down, offset + WIDTH, down + HEIGHT))
        for landmark in LANDMARKS:
            if LEDGES[landmark.last].y - 230 < cy + HEIGHT and LEDGES[landmark.first].y + 380 > cy:
                art, x, y = _landmark_bitmap(landmark)
                _paste(frame, art, x - cx, y - cy)
        visible = ledge_indices(cy - 405, cy + HEIGHT + 25)
        for index in visible:
            ledge = LEDGES[index]
            if not ledge.piece and index % 5 == 2 and ledge.material in self.recesses:
                toward = ledge.x - 152 if ledge.center < WIDTH / 2 else ledge.right - 58
                _paste(frame, self.recesses[ledge.material], toward - cx, ledge.y - cy - 24)
        for index in reversed(visible):
            ledge = LEDGES[index]
            if ledge.piece:
                continue
            texture, top = self._ledge_bitmap(ledge.material, ledge.width, ledge.depth, ledge.slope)
            x, y = ledge.x - cx - 6, ledge.y - cy - top
            if y < HEIGHT and y + texture.height > 0:
                _paste(frame, texture, x, y)
        draw = ImageDraw.Draw(frame)
        for index in visible:
            ledge = LEDGES[index]
            if ledge.piece:
                for color, polygon in ledge_faces(ledge):
                    draw.polygon([(x - cx, y - cy) for x, y in polygon], fill=color)
            if not ledge.slick:
                continue
            start, end = ledge.x + 15, ledge.right - 15
            for spot in range(start, end, 27):
                tail = min(end, spot + 12)
                draw.line((spot - cx, ledge.surface(spot) - cy + 1,
                           tail - cx, ledge.surface(tail) - cy + 2), fill=(117, 193, 202), width=3)
        self._world_details(draw, state, cx, cy)
        x, y = state["x"] - cx, state["y"] - cy
        if state["recovering"]:
            self._rescue(draw, state, x, cy)
        else:
            self._player(frame, draw, state, x, y)
        self._hud(draw, state)
        return frame

    def _world_details(self, draw, state, cx, cy):
        if state["gust"]:
            direction = 1 if state["gust"] > 0 else -1
            speed = abs(state["gust"])
            for index in range(9):
                x = (index * 193 + state["clock"] * direction * (280 + speed)) % WIDTH
                y = 142 + index * 54 + math.sin(index) * 23
                draw.line((x, y, x + direction * (19 + speed * 0.19), y - 6),
                          fill=(181, 192, 195), width=2)
        for index in (*CHECKPOINTS, len(LEDGES) - 1):
            ledge = LEDGES[index]
            x = (ledge.right - 45) - cx
            y = ledge.y - cy
            if not 0 <= y <= HEIGHT:
                continue
            draw.line((x, y + 3, x + 4, y - 51), fill=(80, 61, 46), width=10)
            draw.line((x + 2, y, x + 6, y - 50), fill=(140, 111, 80), width=2)
            if index != 0:
                draw.polygon([(x + 7, y - 50), (x + 40, y - 44), (x + 7, y - 32)],
                             fill=(192, 157, 86) if index != len(LEDGES) - 1 else (217, 188, 121))
            else:
                draw.line((x - 3, y - 26, x + 10, y - 22), fill=(169, 141, 96), width=4)
        water = WATER_Y - cy
        if water < HEIGHT:
            points = [(x, round(water + math.sin(x / 29 + state["clock"] * 2) * 2))
                      for x in range(0, WIDTH + 15, 12)]
            draw.polygon([*points, (WIDTH, HEIGHT), (0, HEIGHT)], fill=(50, 72, 72))
            draw.line(points, fill=(142, 154, 131), width=2)
        effects = state["effects"]
        if "guidance" in effects:
            guide = effects["guidance"]
            x, y = guide["x"] - cx, guide["y"] - cy
            draw.arc((x - 25, y - 12, x + 25, y + 9), 0, 360, fill=SAGE, width=3)
            draw.line((x, y - 20, x, y - 39), fill=SAGE, width=2)
        if "net" in effects:
            net = effects["net"]
            x, y = net["x"] - cx, net["y"] - cy
            points = [(x + t - 153, y + 24 * math.sin(t / 306 * math.pi)) for t in range(0, 307, 17)]
            draw.line(points, fill=SAGE, width=3)
            for px, py in points:
                draw.line((px, py, px, py + 8), fill=(122, 161, 139), width=2)
        plank = effects.get("step", {})
        if "ledge" in plank:
            ledge = plank["ledge"]
            x, y = ledge.x - cx, ledge.y - cy
            draw.rounded_rectangle((x, y - 4, x + ledge.width, y + 10), radius=7,
                                   fill=(104, 139, 147), outline=(200, 225, 220), width=2)
            for i in range(3):
                px = x + 17 + i * 47
                draw.ellipse((px, y + 12, px + 13, y + 25), outline=(172, 200, 203), width=2)
            _text(draw, (x + ledge.width / 2, y - 14), plank["by"], 14, SAGE, "ms")
        boat = effects.get("boat")
        if boat and not state["recovering"]:
            x = 110
            y = min(HEIGHT - 34, WATER_Y - cy) + math.sin(state["clock"] * 3) * 2
            self._boat(draw, x, y)
            _text(draw, (x, y - 46), f"{boat['by']} 号", 15, GOLD, "ms")

    def _player(self, frame, draw, state, x, y):
        facing = state["facing"]
        crouch = state["crouch"]
        sprite = self._player_sprite(facing, round(crouch * 8))
        bob = math.sin(state["clock"] * 6) * (1 if state["standing"] is not None else 2)
        if state["charging"]:
            bob += state["power"] * 6
        if state["standing"] is not None:
            draw.ellipse((x - 62, y - 8, x + 58, y + 7), fill=(78, 67, 50))
        if state["rocket"]:
            self._thruster_flames(draw, state, x, y, state["rocket"], big=True)
        elif state["nitro"]:
            self._thruster_flames(draw, state, x, y, state["nitro"])
        _paste(frame, sprite, x - sprite.width * (0.52 if facing == 1 else 0.48), y - sprite.height + bob)
        cx, cy = state["camera"]
        wx, wy, ax, ay, contact = state["rod"]
        hx, hy, tx, ty = wx - cx, wy - cy, ax - cx, ay - cy
        rod_length = max(1, math.hypot(tx - hx, ty - hy))
        dx, dy = (tx - hx) / rod_length, (ty - hy) / rod_length
        nx, ny = -dy, dx
        shoulder = (x + facing * 24, y - 135 + 37 * crouch + bob)
        elbow = (x + facing * (43 - 28 * crouch), y - 111 + 51 * crouch + dy * 13 + bob)
        cuff = (hx - dx * 9, hy - dy * 9)
        arm = [shoulder, elbow, cuff]
        draw.line(arm, fill=(25, 29, 45), width=15, joint="curve")
        draw.line(arm, fill=(45, 52, 82), width=11, joint="curve")
        draw.line((cuff, (hx, hy)), fill=(235, 225, 222), width=12)
        draw.line((hx, hy, tx, ty), fill=(62, 43, 32), width=10)
        draw.line((hx, hy - 1, tx, ty - 1), fill=(164, 112, 59), width=6)
        draw.line((hx, hy - 3, tx, ty - 3), fill=(223, 171, 99), width=2)
        draw.ellipse((hx - 6, hy - 7, hx + 6, hy + 7), fill=(239, 195, 170), outline=(149, 111, 102))
        if contact and state["rod_normal"]:
            normal_x, normal_y = state["rod_normal"]
            rim_x, rim_y = -normal_y, normal_x
            outer_left = (tx - rim_x * 13 - normal_x * 5, ty - rim_y * 13 - normal_y * 5)
            outer_right = (tx + rim_x * 13 - normal_x * 5, ty + rim_y * 13 - normal_y * 5)
            inner_left = (tx - rim_x * 7 + normal_x * 4, ty - rim_y * 7 + normal_y * 4)
            inner_right = (tx + rim_x * 7 + normal_x * 4, ty + rim_y * 7 + normal_y * 4)
            draw.polygon([outer_left, outer_right, inner_right, inner_left], fill=(48, 50, 45))
            draw.line((outer_left, outer_right), fill=(164, 143, 86), width=2)
        else:
            draw.polygon([(tx - dx * 9 + nx * 5, ty - dy * 9 + ny * 5),
                          (tx - dx * 9 - nx * 5, ty - dy * 9 - ny * 5),
                          (tx - nx * 15, ty - ny * 15),
                          (tx + nx * 15, ty + ny * 15)], fill=(55, 54, 50))
            draw.line((tx - nx * 15, ty - ny * 15, tx + nx * 15, ty + ny * 15),
                      fill=(131, 111, 77), width=3)
        if state["rod_impact"]:
            at, hit = state["rod_impact"]
            age = state["clock"] - at
            for i in range(5):
                travel = 12 + age * (82 + i * 11)
                spread = (i - 2) * travel * 0.34
                px = hit.x - cx + hit.nx * travel - hit.ny * spread
                py = hit.y - cy + hit.ny * travel + hit.nx * spread
                draw.line((px, py, px + hit.nx * 5, py + hit.ny * 5),
                          fill=GOLD if i % 2 else PALE, width=2)
        if "school" in state["effects"]:
            for i in range(5):
                w = state["clock"] * 1.4 + i * 1.3
                px, py = x - facing * (78 + 16 * i), y - 70 + math.sin(w) * 33 + i * 6
                draw.ellipse((px - 6, py - 3, px + 6, py + 3), fill=(181, 198, 185))
                draw.polygon([(px - 3, py), (px - 10, py - 4), (px - 10, py + 4)], fill=(155, 171, 159))

    def _thruster_flames(self, draw, state, x, y, remaining, big=False):
        clock = state["clock"]
        strength = clamp(remaining / (ROCKET_BURN if big else NITRO_BURN), 0.15, 1)
        scale = 1.5 if big else 1.0
        for index in range(5):
            sway = math.sin(clock * 21 + index * 1.7) * 4
            start = x - 26 + index * 12
            length = (24 + (index % 3) * 13) * (0.55 + strength * 0.55) * scale
            tip = start + 3 + sway
            draw.polygon([(start - 6, y + 2), (tip, y + length), (start + 7, y + 2)], fill=ICE if big else CORAL)
            draw.polygon([(start - 2, y + 2), (tip, y + length * 0.62), (start + 3, y + 2)], fill=(255, 236, 190))
        for index in range(3 if big else 2):
            puff = (clock * 130 + index * 34) % 58
            px = x - 16 + index * 16 + math.sin(clock * 9 + index) * 5
            py = y + 10 + puff * scale
            radius = (5 + puff / 10) * scale
            draw.ellipse((px - radius, py - radius, px + radius, py + radius),
                         outline=(151, 154, 146), width=2)

    @lru_cache(maxsize=18)
    def _player_sprite(self, facing, crouch):
        sprite = self.players[facing]
        if not crouch:
            return sprite
        height = round(sprite.height * (1 - 0.025 * crouch))
        return sprite.resize((sprite.width, height), Image.Resampling.LANCZOS)

    def _rescue(self, draw, state, x, cy):
        y = min(HEIGHT - 38, WATER_Y - cy)
        t = state["recovery_left"]
        if not state["boat_riding"]:
            r = 22 + 22 * (1.5 - min(t, 1.5))
            draw.arc((x - r, y - 12, x + r, y + 12), 0, 360, fill=(182, 215, 211), width=3)
            return
        x = x - clamp((t - 0.9) * 420, 0, 500)
        self._boat(draw, x, y)

    def _boat(self, draw, x, y):
        draw.polygon([(x - 65, y - 17), (x + 65, y - 17), (x + 43, y + 10), (x - 46, y + 10)],
                     fill=(150, 91, 46), outline=(225, 183, 116), width=3)
        draw.rectangle((x - 26, y - 42, x + 23, y - 18), fill=(218, 195, 145))
        draw.rectangle((x - 12, y - 38, x + 11, y - 27), fill=(54, 76, 78))
        draw.line((x + 12, y - 43, x + 12, y - 61), fill=(216, 196, 157), width=2)
        draw.polygon([(x + 12, y - 60), (x + 36, y - 53), (x + 12, y - 48)], fill=(185, 103, 62))

    def _gift_panel(self, draw, top, state):
        groups = (
            ("援助", SAGE, HELP_GIFTS),
            ("强力", ICE, POWER_GIFTS),
            ("捣乱", CORAL, SABOTAGE_GIFTS),
            ("关注", GOLD, FOLLOW_EFFECTS),
        )
        names = {**state["gifts"], "nitro": "新关注"}
        effects = state["effects"]
        left, right = 901, 1251
        height = 40
        for _, _, gifts in groups:
            height += 21 + sum(23 + (len(_gift_name_lines(names[kind] or "待绑定")) - 1) * 19
                               for kind in gifts)
        draw.rounded_rectangle((left, top, right, top + height), radius=10,
                               fill=(37, 40, 41), outline=(89, 93, 85), width=1)
        _text(draw, (left + 14, top + 10), "礼物 / 关注 效果", 17, PALE)
        draw.line((left + 14, top + 34, right - 14, top + 34), fill=(84, 88, 80))
        y = top + 42
        for title, color, gifts in groups:
            _text(draw, (left + 14, y), title, 14, color)
            y += 21
            for kind, effect in gifts.items():
                name = names[kind]
                lines = _gift_name_lines(name or "待绑定")
                for offset, line in enumerate(lines):
                    _text(draw, (left + 14, y + offset * 19), line, 16, PALE if name else (174, 178, 167))
                _text(draw, (left + 192, y), "→", 16, SOFT)
                _text(draw, (left + 214, y), effect, 16, color)
                if kind == "nitro" and state["nitro"]:
                    draw.rounded_rectangle((right - 60, y - 2, right - 12, y + 18), radius=7,
                                           fill=(75, 66, 51))
                    _text(draw, (right - 36, y + 8), "加速中", 13, GOLD, "mm")
                elif kind == "nitro":
                    _text(draw, (right - 36, y + 8), "免费", 13, SOFT, "mm")
                elif kind == "rocket" and state["rocket"]:
                    draw.rounded_rectangle((right - 60, y - 2, right - 12, y + 18), radius=7,
                                           fill=(52, 70, 80))
                    _text(draw, (right - 36, y + 8), "喷发中", 13, ICE, "mm")
                elif kind == "magnet" and kind in effects:
                    draw.rounded_rectangle((right - 58, y - 2, right - 12, y + 18), radius=7,
                                           fill=(52, 70, 80))
                    _text(draw, (right - 35, y + 8), f"剩{effects[kind]['jumps']}跳", 13, ICE, "mm")
                elif kind in effects:
                    draw.rounded_rectangle((right - 52, y - 2, right - 12, y + 18), radius=7,
                                           fill=(75, 66, 51))
                    _text(draw, (right - 32, y + 8), "待命", 13, GOLD, "mm")
                y += 23 + (len(lines) - 1) * 19

    def _hud(self, draw, state):
        _text(draw, (31, 27), TITLE, 25)
        _text(draw, (33, 60), f"{state['chapter'] + 1:02d}/{len(CHAPTERS)}  {state['zone']}", 16, SOFT)
        hours, rest = divmod(int(state["clock"]), 3600)
        minutes, seconds = divmod(rest, 60)
        elapsed = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        _text(draw, (33, 85), elapsed, 16, SOFT)
        _text(draw, (33, 107), state["landmark"] or state["terrain"], 17, GOLD)
        _text(draw, (33, 130), f"失手 {state['misses']}   翻回 {state['comebacks']}", 16, SOFT)
        _text(draw, (1247, 30), f"当前 {state['height']:02d} m   最高 {state['best']:02d} m", 19, PALE, "rt")
        guidance, air, net = (state["votes"][name] for name in ("指路", "打气", "接住"))
        effects = state["effects"]
        votes = (f"指路  {'亮灯' if 'guidance' in effects else f'{guidance[0]}/{guidance[1]}'}    "
                 f"打气  {'就绪' if 'air' in effects else f'{air[0]}/{air[1]}'}    "
                 f"接住  {'已张网' if 'net' in effects else f'{net[0]}/{net[1]}'}")
        _text(draw, (1247, 60), votes, 15, SAGE, "rt")
        if state["gust"]:
            _text(draw, (886, 90), "阵风向右" if state["gust"] > 0 else "阵风向左", 16, GOLD, "rt")
        self._gift_panel(draw, 83, state)
        if state["notice"]:
            notice = state["notice"]
            accent = CORAL if any(word in notice for word in ("打滑", "掉回下层", "落水到底", "没够到", "逆风", "捣乱")) else GOLD
            right = max(281, 54 + round(draw.textlength(notice, font=_font(18))))
            draw.rounded_rectangle((26, 619, right, 661), radius=8, fill=INK)
            draw.rounded_rectangle((26, 619, 31, 661), radius=2, fill=accent)
            _text(draw, (43, 640), notice, 18, accent, "lm")
        if state["charging"]:
            x, y = 497, 689
            draw.rounded_rectangle((x, y, x + 285, y + 9), radius=4, fill=(63, 64, 54))
            draw.rounded_rectangle((x, y, x + max(6, round(285 * state["power"])), y + 9),
                                   radius=4, fill=GOLD)
        if state["finished"]:
            draw.rounded_rectangle((455, 270, 825, 418), radius=14, fill=INK, outline=GOLD, width=2)
            _text(draw, (640, 318), "大肥鱼爬出海了", 29, PALE, "mm")
            _text(draw, (640, 365),
                  f"用时 {elapsed}  ·  最大落差 {state['fall']} m", 17, SOFT, "mm")
            _text(draw, (640, 393), f"{state['result_left']} 秒后开始下一局", 15, SOFT, "mm")
        if not state["auto"]:
            _text(draw, (640, 656), "自动攀爬已暂停", 17, PALE, "mm")
