"""The cliff scene shared by the operator page and the native broadcast source."""

from functools import lru_cache
import json
import math
import random
from pathlib import Path
from threading import Lock

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from climb import (FOLLOW_EFFECTS, HEIGHT, HELP_GIFTS, LEDGES, NITRO_BURN, POWER_GIFTS,
                   ROCKET_BURN, SABOTAGE_GIFTS, TITLE, WIDTH, clamp)
from climb_course import BASE_Y, CHAPTERS, CHECKPOINTS, LANDMARKS, WATER_Y, chapter_at, ledge_indices
from climb_landmarks import architecture_faces, ledge_faces
from live_capture import OUT_H, OUT_W


PALE = (247, 238, 214)
SOFT = (220, 217, 203)
GOLD = (224, 188, 112)
SAGE = (169, 212, 186)
CORAL = (247, 166, 143)
ICE = (157, 205, 231)
INK = (48, 47, 43)
ICON_SIZE = 18
GIFT_ROW_H = 22
BACKDROP_BLEND = 20.0
CHAPTER_START = {}
for _ledge in LEDGES:
    CHAPTER_START.setdefault(chapter_at(_ledge.y), round((BASE_Y - _ledge.y) / 25))


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
        self.backdrop_edges = [(40.0, self.background, self.backdrops[0])]
        for index in range(1, len(CHAPTERS)):
            if CHAPTERS[index].backdrop != CHAPTERS[index - 1].backdrop:
                self.backdrop_edges.append(
                    (float(CHAPTER_START[index]), self.backdrops[CHAPTERS[index - 1].backdrop],
                     self.backdrops[CHAPTERS[index].backdrop]))
        types = {ledge.material for ledge in LEDGES}
        self.material = {name: Image.open(terrain / f"{name}.webp").convert("RGBA") for name in types}
        self.recesses = {
            name: ImageEnhance.Brightness(value).enhance(0.46).resize((220, 630), Image.Resampling.LANCZOS)
            for name, value in self.material.items() if name not in {"steel", "wood", "scaffold"}
        }
        original = Image.open(self.assets / "rig-body.webp").convert("RGBA")
        original.thumbnail((225, 202), Image.Resampling.LANCZOS)
        self.players = {1: original, -1: ImageOps.mirror(original)}
        self.gift_icons = {}
        icons = self.assets / "gifts"
        manifest = icons / "icons.json"
        if manifest.exists():
            for name, file in json.loads(manifest.read_text(encoding="utf-8")).items():
                path = icons / file
                if path.exists():
                    icon = Image.open(path).convert("RGBA")
                    icon.thumbnail((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
                    self.gift_icons[name] = icon
        self._ready = True

    def _background(self, state, offset, down):
        height_now = (BASE_Y - state["y"]) / 25
        image, blend, weight = self.backdrop_edges[0][1], None, 1.0
        for edge, old, new in self.backdrop_edges:
            if height_now < edge - BACKDROP_BLEND / 2:
                image = old
                break
            if height_now < edge + BACKDROP_BLEND / 2:
                image, blend = new, old
                weight = clamp((height_now - (edge - BACKDROP_BLEND / 2)) / BACKDROP_BLEND, 0, 1)
                break
            image = new
        box = (offset, down, offset + WIDTH, down + HEIGHT)
        frame = image.crop(box)
        if blend is not None and 0 < weight < 1:
            frame = Image.blend(blend.crop(box), frame, weight)
        return frame

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

    @lru_cache(maxsize=192)
    def _ledge_bitmap(self, material, width, depth, slope, seed=0):
        texture = self.material[material]
        rendered = texture.resize((width + 12, depth + 20), Image.Resampling.LANCZOS)
        pad = abs(slope) // 2 + 3
        if slope:
            rendered = rendered.transform((rendered.width, rendered.height + 2 * pad), Image.Transform.AFFINE,
                                          (1, 0, 0, -slope / rendered.width, 1, slope / 2 - pad),
                                          Image.Resampling.BICUBIC)
        ledge_top = clamp(depth * (0.11 if material in {"wood", "steel", "scaffold"} else 0.09), 4, 28)
        return self._decorate_ledge(rendered, material, width, depth, slope,
                                    ledge_top + (pad if slope else 0), seed)

    def _decorate_ledge(self, image, material, width, depth, slope, surface, seed):
        rng = random.Random((seed + 1) * 7919 + width * 31 + depth * 17 + sum(map(ord, material)))
        alpha = np.array(image)[..., 3]
        pixels = np.array(image)
        opaque = pixels[..., 3] > 50
        average = pixels[opaque][:, :3].mean(axis=0) if opaque.any() else np.array([120, 120, 120])
        base = tuple(int(v) for v in average)
        dark = tuple(int(v * 0.55) for v in base)
        light = tuple(min(255, int(v * 1.22 + 10)) for v in base)
        moss = (108, 133, 84, 215)
        snow_white = (238, 244, 247, 235)
        ice = (196, 226, 238, 232)
        top_pad = 16
        extra = 46 if material in {"limestone", "snow"} else 12
        canvas = Image.new("RGBA", (image.width, image.height + top_pad + extra))
        canvas.paste(image, (0, top_pad))
        draw = ImageDraw.Draw(canvas)
        surface += top_pad
        center = image.width / 2

        def surf(x):
            return surface + (x - center) * (slope / width)

        def bottom(x):
            column = alpha[:, min(max(int(x), 0), alpha.shape[1] - 1)]
            rows = np.nonzero(column > 60)[0]
            return top_pad + int(rows.max()) if len(rows) else surface + depth - 4

        def rock_chip():
            x = rng.uniform(8, image.width - 8)
            y = surf(x) + rng.uniform(4, max(6, depth * 0.5))
            size = rng.randint(3, 7)
            draw.polygon([(x - size, y), (x + size * 0.4, y - size), (x + size, y + size)],
                         fill=(*dark, 150))

        if material == "sandstone":
            for layer in range(rng.randint(2, 4)):
                y0 = surface + depth * (layer + 1) / 5
                points = [(x, y0 + rng.randint(-2, 2) + (x - center) * slope / width)
                          for x in range(6, image.width - 6, 19)]
                draw.line(points, fill=(*dark, 130), width=1)
            for _ in range(rng.randint(2, 5)):
                x = rng.uniform(10, image.width - 10)
                y = surf(x) - rng.randint(2, 7)
                draw.line((x, y, x - rng.randint(1, 3), y - rng.randint(3, 8)), fill=moss, width=1)
        elif material == "granite":
            for _ in range(int(width * depth / 700)):
                x = rng.uniform(7, image.width - 7)
                y = rng.uniform(surface + 6, surface + depth - 3)
                draw.point((x, y), fill=(*dark, 170))
            for _ in range(rng.randint(1, 3)):
                rock_chip()
        elif material == "basalt":
            for _ in range(rng.randint(2, 4)):
                x = rng.uniform(10, image.width - 10)
                y = surf(x) + rng.randint(6, max(8, depth // 3))
                points = [(x, y)]
                for _ in range(rng.randint(2, 4)):
                    x += rng.randint(-14, 14)
                    y += rng.randint(4, 13)
                    points.append((x, y))
                draw.line(points, fill=(*dark, 175), width=1)
            for _ in range(rng.randint(2, 4)):
                x = rng.uniform(12, image.width - 12)
                y = surf(x) - rng.randint(1, 4)
                draw.ellipse((x - 5, y - 2, x + 5, y + 2), fill=moss)
        elif material == "limestone":
            for _ in range(rng.randint(2, 4)):
                x = rng.uniform(12, image.width - 12)
                y = surf(x) + rng.randint(4, 10)
                draw.line((x, y, x, y + rng.randint(10, 26)), fill=(*light, 85), width=2)
            for _ in range(rng.randint(2, 5)):
                x = rng.uniform(12, image.width - 12)
                y = bottom(x) - 1
                length = rng.randint(8, 30)
                half = rng.randint(2, 4)
                draw.polygon([(x - half, y), (x + half, y), (x, y + length)], fill=(*light, 210))
            for _ in range(rng.randint(1, 2)):
                rock_chip()
        elif material == "snow":
            for _ in range(max(3, width // 42)):
                x = rng.uniform(8, image.width - 8)
                y = surf(x)
                rw = rng.randint(10, 26)
                rh = rng.randint(3, 7)
                draw.ellipse((x - rw, y - rh, x + rw, y + rh // 2), fill=snow_white)
            for _ in range(rng.randint(3, 7)):
                x = rng.uniform(10, image.width - 10)
                y = bottom(x) - 1
                draw.polygon([(x - 3, y), (x + 3, y), (x, y + rng.randint(8, 30))], fill=ice)
        elif material == "wood":
            for part in (0.33, 0.66):
                y = surface + depth * part
                draw.line((6, y, image.width - 6, y), fill=(*dark, 190), width=1)
            for _ in range(rng.randint(1, 3)):
                x = rng.uniform(12, image.width - 12)
                y = surface + rng.uniform(0.18, 0.75) * depth
                radius = rng.randint(2, 4)
                draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                             outline=(*dark, 170), width=1)
            if rng.random() < 0.5:
                x = rng.uniform(20, image.width - 20)
                y = surf(x) - 4
                draw.arc((x - 8, y - 7, x + 8, y + 5), 200, 340, fill=(176, 150, 106, 230), width=2)
        elif material == "scaffold":
            for _ in range(rng.randint(1, 3)):
                x0 = rng.uniform(8, image.width * 0.45)
                x1 = min(image.width - 6, x0 + rng.uniform(30, 90))
                draw.line((x0, surface + 6, x1, surface + depth - 4), fill=(*light, 200), width=3)
                draw.line((x0, surface + depth - 4, x1, surface + 6), fill=(*light, 200), width=3)
            draw.line((6, surface + 3, 6, surface + depth), fill=(*dark, 180), width=2)
            draw.line((image.width - 6, surface + 3, image.width - 6, surface + depth),
                      fill=(*dark, 180), width=2)
        elif material == "steel":
            draw.line((6, surface + depth * 0.3, image.width - 6, surface + depth * 0.3),
                      fill=(*dark, 190), width=2)
            for x in range(12, image.width - 8, 27):
                draw.ellipse((x - 2, surface + 6, x + 2, surface + 10), fill=(*light, 235))
                if rng.random() < 0.5:
                    draw.line((x, surface + 14, x, surface + rng.randint(20, max(22, depth - 6))),
                              fill=(150, 86, 56, 135), width=2)
            for _ in range(rng.randint(1, 3)):
                x = rng.uniform(10, image.width - 10)
                y0 = surface + rng.randint(12, max(14, depth - 6))
                draw.line((x, y0, x + rng.randint(-6, 6), min(surface + depth - 2, y0 + rng.randint(8, 20))),
                          fill=(120, 68, 46, 90), width=1)

        roll = rng.random()
        if material in {"snow", "limestone"} and roll < 0.28:
            x = rng.uniform(18, image.width - 18)
            y = surf(x)
            draw.polygon([(x - 5, y - 1), (x + 5, y - 2), (x, y - rng.randint(10, 16))], fill=ice)
        elif material in {"wood", "scaffold"} and roll < 0.26:
            x = rng.uniform(18, image.width - 18)
            y = surf(x) - 1
            draw.rectangle((x - 3, y - 15, x + 3, y - 9), fill=(198, 164, 108, 235))
            draw.line((x, y - 15, x, y - 20), fill=(120, 96, 70, 235), width=1)
        elif material in {"granite", "basalt"} and roll < 0.24:
            for _ in range(rng.randint(2, 4)):
                x = rng.uniform(10, image.width - 10)
                y = surf(x) + rng.uniform(2, 8)
                draw.ellipse((x - 2, y - 1, x + 2, y + 1), fill=(196, 164, 88, 220))
        elif material in {"sandstone"} and roll < 0.24:
            x = rng.uniform(18, image.width - 18)
            y = surf(x)
            draw.rectangle((x - 2, y - 9, x + 2, y), fill=(224, 213, 196, 230))
            draw.ellipse((x - 3, y - 12, x + 3, y - 7), fill=(150, 74, 62, 230))

        return canvas, surface

    def _compose(self, state):
        cx, cy = state["camera"]
        offset = round(clamp(78 + cx * 0.23, 0, 160))
        down = round(clamp(75 + 16 * math.sin(cy / 680), 0, 90))
        frame = self._background(state, offset, down)
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
            texture, top = self._ledge_bitmap(ledge.material, ledge.width, ledge.depth, ledge.slope, index)
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
        self._hud(draw, frame, state)
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
        chapter, clock = state["chapter"], state["clock"]
        if chapter in (8, 9):
            for index in range(24):
                x = (index * 89 + clock * 21) % WIDTH
                y = (index * 137 + clock * 33) % HEIGHT
                draw.line((x, y, x - 3, y + 6), fill=(234, 240, 244), width=1)
        elif chapter in (6, 7):
            for index in range(12):
                x = (index * 157 + clock * 29) % WIDTH
                y = (index * 211 + clock * 11) % HEIGHT
                draw.ellipse((x, y, x + 3, y + 2), fill=(168, 118, 82))
        elif chapter in (4, 5):
            for index in range(10):
                x = (index * 193 + clock * 9) % WIDTH
                y = (index * 173 + clock * 47) % HEIGHT
                draw.line((x, y, x, y + 9), fill=(130, 158, 166), width=1)
        elif chapter in (10, 11):
            for index in range(6):
                x = (index * 241 + clock * 15) % WIDTH
                y = 90 + index * 87 + math.sin(clock * 0.7 + index) * 18
                draw.line((x, y, x + 26, y - 4), fill=(206, 214, 216), width=1)
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

    def _gift_panel(self, draw, frame, top, state):
        groups = (
            ("援助", SAGE, HELP_GIFTS),
            ("强力", ICE, POWER_GIFTS),
            ("捣乱", CORAL, SABOTAGE_GIFTS),
            ("关注", GOLD, FOLLOW_EFFECTS),
        )
        names = {**state["gifts"], "nitro": "新关注"}
        effects = state["effects"]
        right = 1247
        effect_width = round(max(_font(14).getlength(effect)
                                 for _, _, gifts in groups for effect in gifts.values()))
        effect_x = right - 48
        icon_x = effect_x - effect_width - ICON_SIZE - 5
        y = top
        for title, color, gifts in groups:
            _text(draw, (icon_x, y), title, 12, color)
            y += 16
            for kind, effect in gifts.items():
                name = names[kind]
                if kind == "nitro":
                    draw.ellipse((icon_x, y + 1, icon_x + ICON_SIZE - 1, y + ICON_SIZE),
                                 outline=GOLD, width=2)
                    _text(draw, (icon_x + ICON_SIZE // 2, y + 1 + ICON_SIZE // 2),
                          "+", 11, GOLD, "mm")
                else:
                    icon = self.gift_icons.get(name) if name else None
                    if icon is not None:
                        _paste(frame, icon, icon_x, y + 1)
                    else:
                        draw.ellipse((icon_x, y + 1, icon_x + ICON_SIZE - 1, y + ICON_SIZE),
                                     outline=(150, 150, 140), width=1)
                        _text(draw, (icon_x + ICON_SIZE // 2, y + 1 + ICON_SIZE // 2),
                              "?", 10, (170, 170, 160), "mm")
                center = y + 1 + ICON_SIZE // 2
                _text(draw, (effect_x, center), effect, 14, color, "rm")
                status = None
                status_color = SOFT
                if kind == "nitro" and state["nitro"]:
                    status, status_color = "加速中", GOLD
                elif kind == "nitro":
                    status, status_color = "免费", SOFT
                elif kind == "rocket" and state["rocket"]:
                    status, status_color = "喷发中", ICE
                elif kind == "magnet" and kind in effects:
                    status, status_color = f"剩{effects[kind]['jumps']}跳", ICE
                elif kind in effects:
                    status, status_color = "待命", GOLD
                if status:
                    _text(draw, (right, center), status, 11, status_color, "rm")
                y += GIFT_ROW_H

    def _hud(self, draw, frame, state):
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
        self._gift_panel(draw, frame, 83, state)
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
