"""The cliff scene shared by the operator page and the native broadcast source."""

from functools import lru_cache
import math
from pathlib import Path
from threading import Lock

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from climb import HEIGHT, LEDGES, TITLE, WIDTH, clamp
from climb_course import CHAPTERS, CHECKPOINTS, ledge_indices
from live_capture import OUT_H, OUT_W


PALE = (247, 238, 214)
SOFT = (220, 217, 203)
GOLD = (224, 188, 112)
SAGE = (169, 212, 186)
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
        visible = ledge_indices(cy - 405, cy + HEIGHT + 25)
        for index in visible:
            ledge = LEDGES[index]
            if index % 5 == 2 and ledge.material in self.recesses:
                toward = ledge.x - 152 if ledge.center < WIDTH / 2 else ledge.right - 58
                _paste(frame, self.recesses[ledge.material], toward - cx, ledge.y - cy - 24)
        for index in reversed(visible):
            ledge = LEDGES[index]
            texture, top = self._ledge_bitmap(ledge.material, ledge.width, ledge.depth, ledge.slope)
            x, y = ledge.x - cx - 6, ledge.y - cy - top
            if y < HEIGHT and y + texture.height > 0:
                _paste(frame, texture, x, y)
        draw = ImageDraw.Draw(frame)
        self._world_details(draw, state, cx, cy)
        x, y = state["x"] - cx, state["y"] - cy
        if state["tether"]:
            anchor = state["tether"]
            ax, ay = anchor.center - cx, anchor.y - cy
            y = min(HEIGHT - 33, y)
            end = (x - state["facing"] * 22, y - 46)
            line = [(ax, ay), (ax * 0.35 + end[0] * 0.65, ay * 0.42 + end[1] * 0.58 + 15), end]
            draw.line(line, fill=(36, 35, 30), width=5)
            draw.line(line, fill=GOLD, width=2)
            self._player(frame, draw, state, x, y)
        elif state["recovering"]:
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
        water = BASE_WATER_Y - cy
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
            y = min(HEIGHT - 34, BASE_WATER_Y - cy) + math.sin(state["clock"] * 3) * 2
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

    @lru_cache(maxsize=18)
    def _player_sprite(self, facing, crouch):
        sprite = self.players[facing]
        if not crouch:
            return sprite
        height = round(sprite.height * (1 - 0.025 * crouch))
        return sprite.resize((sprite.width, height), Image.Resampling.LANCZOS)

    def _rescue(self, draw, state, x, cy):
        y = min(HEIGHT - 38, BASE_WATER_Y - cy)
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

    def _hud(self, draw, state):
        _text(draw, (31, 27), TITLE, 25)
        _text(draw, (33, 60), f"{state['chapter'] + 1:02d}/{len(CHAPTERS)}  {state['zone']}", 16, SOFT)
        hours, rest = divmod(int(state["clock"]), 3600)
        minutes, seconds = divmod(rest, 60)
        elapsed = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        _text(draw, (33, 85), elapsed, 16, SOFT)
        _text(draw, (33, 107), f"难度 {state['chapter'] + 1}/{len(CHAPTERS)}", 15, GOLD)
        _text(draw, (1247, 30), f"当前 {state['height']:02d} m   最高 {state['best']:02d} m", 19, PALE, "rt")
        guidance, air, net = (state["votes"][name] for name in ("指路", "打气", "接住"))
        effects = state["effects"]
        votes = (f"指路  {'亮灯' if 'guidance' in effects else f'{guidance[0]}/{guidance[1]}'}    "
                 f"打气  {'就绪' if 'air' in effects else f'{air[0]}/{air[1]}'}    "
                 f"接住  {'已张网' if 'net' in effects else f'{net[0]}/{net[1]}'}")
        _text(draw, (1247, 60), votes, 15, SAGE, "rt")
        gift_bits = []
        for kind, label in (("boat", "打捞艇"), ("step", "泡泡踏板"), ("school", "鱼群跟拍")):
            if kind in effects:
                gift_bits.append(f"{label}待命")
            elif state["gifts"][kind]:
                gift_bits.append(f"{state['gifts'][kind]} → {label}")
        if gift_bits:
            _text(draw, (1247, 84), "   ·   ".join(gift_bits)[:58], 14, GOLD, "rt")
        if state["notice"]:
            _text(draw, (33, 672), state["notice"], 19, PALE, "ls")
        if state["gust"]:
            _text(draw, (1247, 108), "阵风向右" if state["gust"] > 0 else "阵风向左", 17, GOLD, "rt")
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


BASE_WATER_Y = 704
