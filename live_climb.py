"""The cliff scene shared by the operator page and the native broadcast source."""

from functools import lru_cache
import json
import math
import random
from pathlib import Path
from threading import Lock

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

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
WEATHER_COLORS = {"clear": SAGE, "tail": GOLD, "head": CORAL, "gusty": GOLD,
                  "storm": ICE, "fog": SOFT, "blizzard": ICE}
WIND_COLORS = {"tail": (198, 214, 190), "head": (232, 188, 168), "gusty": (216, 218, 208),
               "storm": (180, 198, 210), "blizzard": (232, 240, 244)}
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
        self.fog_banks = ((self._fog_bank(271, 1520, 340, 46), 48, 9),
                          (self._fog_bank(613, 1520, 300, 38), 236, 15),
                          (self._fog_bank(947, 1520, 260, 30), 408, 23))
        self.storm_vignette = self._vignette((8, 15, 23), 62, 62, 26)
        self.storm_base = self._stack(self._storm_shade(),
                                      self._bottom_mist((98, 119, 134), 55, 240))
        self.fog_base = self._stack(self._bottom_mist((207, 212, 207), 56, 300),
                                    self._vignette((46, 52, 49), 30, 72, 30))
        self.blizzard_base = self._stack(self._bottom_mist((236, 243, 247), 56, 250),
                                         self._frost_edge())
        self.snowflakes = {2: self._flake(2, 120, 0.8), 3: self._flake(3, 150, 1.1),
                           5: self._flake(5, 135, 1.8), 8: self._flake(8, 125, 3.2),
                           12: self._flake(12, 85, 5.2)}
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

    @staticmethod
    def _fog_bank(seed, width, height, count):
        rng = random.Random(seed)
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for _ in range(count):
            center_x = rng.uniform(0, width)
            center_y = rng.uniform(height * 0.25, height * 0.75)
            spread = rng.uniform(150, 520)
            tall = rng.uniform(36, 130)
            draw.ellipse((center_x - spread / 2, center_y - tall / 2,
                          center_x + spread / 2, center_y + tall / 2),
                         fill=(219, 224, 220, rng.randint(18, 42)))
        pixels = np.array(layer.filter(ImageFilter.GaussianBlur(20)))
        fade_x = np.ones(width)
        fade_x[:70] = np.linspace(0, 1, 70)
        fade_x[-70:] = np.linspace(1, 0, 70)
        fade_y = np.ones(height)
        fade_y[:60] = np.linspace(0, 1, 60)
        fade_y[-60:] = np.linspace(1, 0, 60)
        pixels[..., 3] = (pixels[..., 3] * fade_x[None, :] * fade_y[:, None]).astype(np.uint8)
        return Image.fromarray(pixels)

    @staticmethod
    def _storm_shade():
        layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for y in range(0, 380, 4):
            alpha = int(52 * (1 - y / 380) ** 1.35)
            if alpha:
                draw.rectangle((0, y, WIDTH, y + 4), fill=(22, 30, 38, alpha))
        return layer.filter(ImageFilter.GaussianBlur(7))

    @staticmethod
    def _stack(*layers):
        image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        for layer in layers:
            image = Image.alpha_composite(image, layer)
        return image

    @staticmethod
    def _vignette(color, alpha, width, blur):
        layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rectangle((0, 0, WIDTH - 1, HEIGHT - 1),
                                        outline=(*color, alpha), width=width)
        return layer.filter(ImageFilter.GaussianBlur(blur))

    @staticmethod
    def _bottom_mist(color, alpha, height):
        layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for step in range(height):
            draw.line((0, HEIGHT - height + step, WIDTH, HEIGHT - height + step),
                      fill=(*color, int(alpha * (step / height) ** 2)))
        return layer.filter(ImageFilter.GaussianBlur(18))

    @staticmethod
    def _frost_edge():
        layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rectangle((3, 3, WIDTH - 4, HEIGHT - 4),
                                        outline=(239, 247, 251, 104), width=34)
        return layer.filter(ImageFilter.GaussianBlur(17))

    @staticmethod
    def _flake(radius, alpha, blur):
        size = radius * 2 + 10
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse((5, 5, size - 5, size - 5), fill=(246, 249, 251, alpha))
        return layer.filter(ImageFilter.GaussianBlur(blur))

    def _weather_grade(self, frame, kind, prev, mix):
        grades = {"storm": (0.78, 0.84, (52, 64, 74), 0.18),
                  "blizzard": (0.88, 0.94, (228, 235, 238), 0.16),
                  "fog": (0.72, 1.0, (208, 212, 207), 0.26),
                  "head": (0.88, 0.93, None, 0),
                  "gusty": (0.94, 1.0, None, 0)}
        start = grades.get(prev, (1.0, 1.0, None, 0))
        end = grades.get(kind, (1.0, 1.0, None, 0))
        blend = mix if prev != kind and mix < 1 else 1
        saturation = start[0] + (end[0] - start[0]) * blend
        brightness = start[1] + (end[1] - start[1]) * blend
        if saturation != 1:
            frame = ImageEnhance.Color(frame).enhance(saturation)
        if brightness != 1:
            frame = ImageEnhance.Brightness(frame).enhance(brightness)
        if start[2] and end[2]:
            tint = tuple(round(a + (b - a) * blend) for a, b in zip(start[2], end[2]))
            alpha = start[3] + (end[3] - start[3]) * blend
        elif end[2]:
            tint, alpha = end[2], end[3] * blend
        elif start[2]:
            tint, alpha = start[2], start[3] * (1 - blend)
        else:
            tint = alpha = 0
        if tint and alpha > 0:
            frame = Image.blend(frame, Image.new("RGB", frame.size, tint), alpha)
        return frame

    def _weather_layer(self, state, kind, density=1):
        if kind == "clear":
            if abs(state["wind"]) < 70:
                return None
            overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            self._wind_streaks(ImageDraw.Draw(overlay), state, kind, density)
            return overlay
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        if kind == "storm":
            overlay.paste(self.storm_base, (0, 0), self.storm_base)
            self._rain(draw, state, density)
            self._wet_rock(draw, state, density)
            self._wind_streaks(draw, state, kind, density)
            overlay.paste(self.storm_vignette, (0, 0), self.storm_vignette)
            self._lightning(draw, state)
        elif kind == "fog":
            self._fog_banks(overlay, state, density)
            overlay.paste(self.fog_base, (0, 0), self.fog_base)
            self._fog_haze(draw)
        elif kind == "blizzard":
            self._snow(overlay, state, density)
            self._snow_streaks(draw, state, density)
            self._wind_streaks(draw, state, kind, density)
            self._snow_rock(draw, state, density)
            overlay.paste(self.blizzard_base, (0, 0), self.blizzard_base)
        else:
            self._wind_streaks(draw, state, kind, density)
            if kind in ("tail", "head", "gusty"):
                self._wind_dust(draw, state, kind, density)
        return overlay

    def _weather_overlay(self, frame, state, kind, prev, mix):
        layers = []
        if prev != kind and mix < 1:
            layers.append((prev, 1 - mix))
        layers.append((kind, mix if prev != kind and mix < 1 else 1))
        for key, strength in layers:
            if strength <= 0:
                continue
            layer = self._weather_layer(state, key, strength)
            if layer is None:
                continue
            if strength < 0.999:
                layer.putalpha(layer.getchannel("A").point(
                    lambda value, ratio=strength: round(value * ratio)))
            frame.paste(layer, (0, 0), layer)
        return frame

    def _wind_streaks(self, draw, state, kind, density=1):
        clock, wind = state["clock"], state["wind"]
        direction = 1 if wind >= 0 else -1
        color = WIND_COLORS.get(kind, (206, 215, 211))
        fade = 0.72 if kind == "storm" else 0.85 if kind == "clear" else 1
        bands = ((22, 28, 72, round(68 * fade), 0.6),
                 (12, 80, 170, round(105 * fade), 0.9),
                 (5, 150, 260, round(40 * fade), 1.3))
        for band, (count, low, high, alpha, speed) in enumerate(bands):
            for index in range(round(count * density)):
                seed = (index * 2654435761 + band * 2246822519) & 0xFFFFFFFF
                y = 80 + (seed >> 5) % 1000 / 1000 * (HEIGHT - 190)
                length = low + (seed >> 15) % (high - low)
                x = (seed % (WIDTH + 260) - 130 + clock * direction
                     * (150 + abs(wind)) * speed) % (WIDTH + 300) - 150
                wobble = math.sin(clock * 1.2 + (seed >> 9) % 9) * (2 + band * 2)
                points = ((x, y), (x + length * 0.35, y + wobble * 0.4),
                          (x + length * 0.7, y - wobble * 0.3), (x + length, y))
                if band == 2:
                    draw.line(points, fill=(*color, 24), width=6)
                draw.line(points, fill=(*color, alpha), width=1 + band)
                if band == 2 and kind in ("tail", "head"):
                    draw.line((x + length, y, x + length - direction * 7, y - 3),
                              fill=(*color, alpha), width=1)
                    draw.line((x + length, y, x + length - direction * 7, y + 3),
                              fill=(*color, alpha), width=1)

    def _wind_dust(self, draw, state, kind, density=1):
        clock, wind = state["clock"], state["wind"]
        direction = 1 if wind >= 0 else -1
        color = {"tail": (214, 201, 156), "head": (224, 184, 166),
                 "gusty": (178, 174, 156)}[kind]
        for index in range(round(26 * density)):
            seed = (index * 1103515245 + 12345) & 0x7FFFFFFF
            x = (seed % (WIDTH + 40) - 20 + clock * direction * (120 + (seed >> 8) % 160)) % (WIDTH + 60) - 30
            y = 120 + (seed >> 13) % 1000 / 1000 * (HEIGHT - 220) + math.sin(clock + index) * 5
            radius = 1 + (seed >> 20) % 2
            draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                         fill=(*color, 80 + (seed >> 22) % 50))
        if kind == "gusty":
            for index in range(round(7 * density)):
                seed = (index * 2246822519 + 13) & 0xFFFFFFFF
                x = (seed % (WIDTH + 40) - 20 + clock * direction
                     * (150 + (seed >> 7) % 120)) % (WIDTH + 60) - 30
                y = 140 + (seed >> 15) % 1000 / 1000 * (HEIGHT - 260)
                angle = clock * 3.6 + index * 1.7
                dx, dy = 4.5 * math.cos(angle), 3 * math.sin(angle)
                draw.polygon(((x, y), (x + dx, y + dy), (x + dx * 2, y), (x + dx, y - dy)),
                             fill=(110, 100, 72, 165))

    def _rain(self, draw, state, density=1):
        clock, direction = state["clock"], 1 if state["wind"] >= 0 else -1
        for band, (count, speed, length, width, alpha, color) in enumerate((
                (54, 780, 9, 1, 55, (171, 193, 208)),
                (42, 1320, 17, 1, 95, (182, 203, 216)),
                (26, 1950, 29, 2, 140, (190, 212, 224)))):
            for index in range(round(count * density)):
                seed = (index * 2654435761 + band * 40503) & 0xFFFFFFFF
                x = seed % (WIDTH + 100) - 50
                y = ((seed >> 7) % 1000 / 1000 * (HEIGHT + 80) + clock * speed) % (HEIGHT + length + 80) - length
                slant = direction * (length * 0.2 + abs(state["wind"]) * 0.02)
                draw.line((x, y, x + slant, y + length), fill=(*color, alpha), width=width)

    def _wet_rock(self, draw, state, density=1):
        cx, cy = state["camera"]
        clock = state["clock"]
        for index in ledge_indices(cy - 60, cy + HEIGHT + 40):
            ledge = LEDGES[index]
            for step in range(2 if density >= 0.5 else 1):
                seed = index * 97 + step * 211
                x = ledge.x + 14 + seed % max(14, ledge.width - 28)
                y = ledge.surface(x) - cy + 2
                if not -16 < y < HEIGHT + 16:
                    continue
                half = 4 + int(5 * ((clock * 0.7 + index * 0.23 + step * 0.41) % 1))
                draw.line((x - cx - half, y, x - cx + half, y),
                          fill=(151, 196, 206, 65), width=1)
                phase = (clock * 1.6 + index * 0.31 + step * 0.47) % 1
                if phase < 0.22:
                    rise = 2 + int(5 * (1 - phase / 0.22))
                    draw.line((x - cx - 2, y, x - cx, y - rise), fill=(190, 215, 225, 140), width=1)
                    draw.line((x - cx + 2, y, x - cx, y - rise), fill=(190, 215, 225, 140), width=1)
                ripple = (clock * 0.75 + index * 0.27 + step * 0.39) % 1
                if ripple < 0.6:
                    radius = 1 + int(10 * ripple / 0.6)
                    alpha = int(52 * (1 - ripple / 0.6))
                    draw.arc((x - cx - radius, y - radius // 2, x - cx + radius, y + radius // 2),
                             0, 360, fill=(181, 214, 223, alpha), width=1)
            if density >= 0.35:
                seed = index * 131 + 7
                x = ledge.x + 12 + seed % max(12, ledge.width - 24)
                under = ledge.underside(ledge.surface(x)) - cy + 4
                drop = 2 + int(6 * ((clock * 0.8 + index * 0.63) % 1))
                if 0 < under < HEIGHT:
                    draw.line((x - cx, under, x - cx, under + drop), fill=(169, 204, 215, 110), width=1)

    def _snow(self, overlay, state, density=1):
        clock, direction = state["clock"], 1 if state["wind"] >= 0 else -1
        for band, (count, radius, speed) in enumerate(((52, 2, 55), (34, 3, 100),
                                                       (12, 8, 155), (5, 12, 195))):
            sprite = self.snowflakes[radius]
            for index in range(round(count * density)):
                seed = (index * 2654435761 + band * 977) & 0xFFFFFFFF
                x = (seed % (WIDTH + 60) - 30 + clock * direction * speed * 0.55
                     + math.sin(clock * 0.9 + seed) * (6 + band * 7)) % (WIDTH + 80) - 40
                y = ((seed >> 9) % 1000 / 1000 * (HEIGHT + 60) + clock * speed) % (HEIGHT + 80) - 40
                overlay.paste(sprite, (int(x), int(y)), sprite)

    def _snow_streaks(self, draw, state, density=1):
        clock, direction = state["clock"], 1 if state["wind"] >= 0 else -1
        for index in range(round(18 * density)):
            seed = (index * 40503 + 977) & 0xFFFFFFFF
            x = (seed % (WIDTH + 60) - 30 + clock * direction * 760) % (WIDTH + 90) - 40
            y = ((seed >> 8) % 1000 / 1000 * (HEIGHT + 60) + clock * 680) % (HEIGHT + 80) - 40
            length = 10 + (seed >> 18) % 14
            draw.line((x, y, x + direction * (3 + length * 0.25), y + length),
                      fill=(244, 248, 251, 95), width=1)

    def _snow_rock(self, draw, state, density=1):
        cx, cy = state["camera"]
        for index in ledge_indices(cy - 40, cy + HEIGHT + 30):
            ledge = LEDGES[index]
            left, right = ledge.x + 10, ledge.right - 10
            draw.line((left - cx, ledge.surface(left) - cy,
                       right - cx, ledge.surface(right) - cy),
                      fill=(238, 246, 249, 75), width=2)
            for step in range(2 if density >= 0.45 else 1):
                x = ledge.x + 18 + (index * 79 + step * 157) % max(16, ledge.width - 36)
                y = ledge.surface(x) - cy
                draw.ellipse((x - cx - 9, y - 2, x - cx + 9, y + 3),
                             fill=(242, 248, 250, 95))

    def _fog_banks(self, overlay, state, density=1):
        clock, direction = state["clock"], 1 if state["wind"] >= 0 else -1
        banks = self.fog_banks[:1] if density < 0.35 else self.fog_banks[:2] if density < 0.7 else self.fog_banks
        for bank, y, speed in banks:
            x = -int((clock * speed * direction) % bank.width)
            overlay.paste(bank, (x, y), bank)
            overlay.paste(bank, (x + bank.width, y), bank)

    @staticmethod
    def _fog_haze(draw):
        draw.rectangle((0, 0, WIDTH, 74), fill=(220, 225, 221, 16))
        draw.rectangle((0, HEIGHT - 118, WIDTH, HEIGHT), fill=(218, 224, 220, 24))

    def _lightning(self, draw, state):
        elapsed = state["clock"] - state["weather"].get("since", 0)
        phase = (elapsed + 1.7) % 8.8
        if phase >= 0.38:
            return
        strength = 96 if phase < 0.07 else 60 if phase < 0.15 else 26 if phase < 0.26 else 10
        for y in range(0, HEIGHT, 4):
            alpha = int(strength * (0.12 + 0.88 * (1 - y / HEIGHT) ** 1.7))
            draw.line((0, y, WIDTH, y), fill=(236, 244, 255, alpha), width=4)
        if phase < 0.12:
            rng = random.Random(int((elapsed + 1.7) // 8.8) * 977 + 19)
            x = rng.randint(210, WIDTH - 210)
            points = [(x, 0)]
            for step in range(1, 4):
                x += rng.randint(-64, 64)
                points.append((x, step * 52 + rng.randint(-14, 14)))
            origin = points[0]
            draw.ellipse((origin[0] - 150, -82, origin[0] + 150, 92), fill=(202, 222, 255, 34))
            draw.line(points, fill=(188, 214, 255, 45), width=5)
            draw.line(points, fill=(248, 251, 255, 175), width=1)
            fork = points[2]
            draw.line((fork, (fork[0] + rng.randint(-54, 54), fork[1] + rng.randint(24, 52))),
                      fill=(196, 220, 255, 125), width=1)

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
        weather = state.get("weather") or {}
        enabled = weather.get("enabled", True)
        kind = weather.get("kind", "clear") if enabled else "clear"
        prev = weather.get("prev", "clear") if enabled else "clear"
        mix = float(weather.get("mix", 1)) if enabled else 1
        frame = self._weather_grade(self._background(state, offset, down), kind, prev, mix)
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
        frame = self._weather_overlay(frame, state, kind, prev, mix)
        self._hud(ImageDraw.Draw(frame), frame, state)
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

    def _weather_icon(self, draw, x, y, kind, color):
        cx, cy = x + 8, y + 8
        if kind == "clear":
            draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), outline=color, width=1)
            for dx, dy in ((0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)):
                draw.line((cx + dx * 5, cy + dy * 5, cx + dx * 7, cy + dy * 7), fill=color)
        elif kind in ("tail", "head"):
            direction = 1 if kind == "tail" else -1
            for offset in (4, 8, 12):
                start, end = (x + 2, x + 12) if direction > 0 else (x + 12, x + 2)
                draw.line((start, y + offset, end, y + offset), fill=color)
                draw.line((end, y + offset, end - direction * 3, y + offset - 2), fill=color)
                draw.line((end, y + offset, end - direction * 3, y + offset + 2), fill=color)
        elif kind == "gusty":
            for offset in (5, 9, 13):
                draw.line(((x + 1, y + offset), (x + 5, y + offset - 2),
                           (x + 9, y + offset + 1), (x + 13, y + offset - 1)),
                          fill=color, joint="curve")
        elif kind == "storm":
            draw.arc((x + 1, y + 1, x + 13, y + 10), 180, 360, fill=color)
            draw.line((x + 1, y + 6, x + 13, y + 6), fill=color)
            draw.line((x + 8, y + 7, x + 5, y + 12, x + 9, y + 12, x + 7, y + 16), fill=color)
        elif kind == "fog":
            for offset in (4, 8, 12):
                draw.line(((x + 1, y + offset), (x + 5, y + offset - 1),
                           (x + 9, y + offset + 1), (x + 14, y + offset)),
                          fill=color, joint="curve")
        elif kind == "blizzard":
            for angle in (0, math.pi / 3, 2 * math.pi / 3):
                dx, dy = math.cos(angle) * 6, math.sin(angle) * 6
                draw.line((cx - dx, cy - dy, cx + dx, cy + dy), fill=color)
                for side in (-1, 1):
                    tip_x, tip_y = cx + dx * side, cy + dy * side
                    draw.line((tip_x, tip_y, tip_x - dx * 0.35 - dy * 0.25,
                               tip_y - dy * 0.35 + dx * 0.25), fill=color)
                    draw.line((tip_x, tip_y, tip_x - dx * 0.35 + dy * 0.25,
                               tip_y - dy * 0.35 - dx * 0.25), fill=color)

    def _hud(self, draw, frame, state):
        _text(draw, (31, 27), TITLE, 25)
        _text(draw, (33, 60), f"{state['chapter'] + 1:02d}/{len(CHAPTERS)}  {state['zone']}", 16, SOFT)
        hours, rest = divmod(int(state["clock"]), 3600)
        minutes, seconds = divmod(rest, 60)
        elapsed = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        _text(draw, (33, 85), elapsed, 16, SOFT)
        _text(draw, (33, 107), state["landmark"] or state["terrain"], 17, GOLD)
        _text(draw, (33, 130), f"失手 {state['misses']}   翻回 {state['comebacks']}", 16, SOFT)
        weather = state.get("weather") or {}
        if weather.get("enabled"):
            color = WEATHER_COLORS.get(weather.get("kind"), SOFT)
            self._weather_icon(draw, 33, 152, weather.get("kind", "clear"), color)
            _text(draw, (57, 152), f"天气 {weather.get('name', '')} · {weather.get('hint', '')}",
                  15, color)
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
