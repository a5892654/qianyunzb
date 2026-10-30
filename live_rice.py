"""直播白饭计数与透明帧合成；不会向游戏写入任何状态。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
import uuid

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import config

ANIMATION_SECONDS = 2.4
WIDTH, HEIGHT = 360, 168


class RiceCounter:
    def __init__(self, path=None):
        self.path = Path(path or os.path.join(config.BASE_DIR, "rice_counter.json"))
        self.lock = threading.RLock()
        self.total = 0
        self.seen = set()
        self.session = uuid.uuid4().hex
        self.event_id = ""
        self.celebrated_at = 0.0
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.total = max(0, int(data.get("total", 0)))
            self.seen = set(data.get("seen", []))
            self.session = str(data.get("session") or self.session)

    def _save(self, total, seen, session):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"total": total, "seen": sorted(seen), "session": session}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def begin_run(self):
        with self.lock:
            session = uuid.uuid4().hex
            self._save(self.total, self.seen, session)
            self.session = session

    def award(self, key):
        with self.lock:
            if key in self.seen:
                return False
            seen = self.seen | {key}
            self._save(self.total + 1, seen, self.session)
            self.total += 1
            self.seen = seen
            self.preview()
            return True

    def preview(self):
        """试播动画不计入真实战绩，也不写入累计文件。"""
        with self.lock:
            self.event_id = uuid.uuid4().hex
            self.celebrated_at = time.time()

    def state(self, enabled=False):
        with self.lock:
            return {"enabled": bool(enabled), "total": self.total, "event_id": self.event_id,
                    "celebrated_at": self.celebrated_at}


class RiceCompositor:
    """在现有采集帧最后叠加白饭；无需启动浏览器或启用 Live2D。"""
    def __init__(self, counter, settings):
        self.counter, self.settings = counter, settings
        self._tiles = None
        self._cache_key = None
        self._sheet = None

    def _render_tiles(self, count, target_h):
        if self._sheet is None:
            self._sheet = Image.open(os.path.join(config.WEB_DIR, "assets", "rice_bowl_celebrate.png")).convert("RGBA")
        font_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyh.ttc")
        title_font = ImageFont.truetype(font_path, 18)
        number_font = ImageFont.truetype(font_path, 42)
        small_font = ImageFont.truetype(font_path, 13)
        plus_font = ImageFont.truetype(font_path, 23)
        cw, ch = self._sheet.width // 4, self._sheet.height // 2
        tiles = []
        for i in range(8):
            image = Image.new("RGBA", (WIDTH, HEIGHT))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((0, 25, WIDTH - 1, HEIGHT - 1), radius=14, fill=(22, 25, 36, 228), outline=(206, 175, 120, 210), width=2)
            bowl = self._sheet.crop(((i % 4) * cw, (i // 4) * ch, (i % 4 + 1) * cw, (i // 4 + 1) * ch))
            image.alpha_composite(bowl.resize((134, 134), Image.Resampling.NEAREST), (3, 29))
            draw.text((145, 42), "大碗白饭", font=title_font, fill=(231, 212, 176, 255))
            draw.text((142, 66), f"{count} 碗", font=number_font, fill=(255, 245, 216, 255))
            draw.text((145, 128), "AI 通关 · 开饭！", font=small_font, fill=(188, 180, 167, 255))
            size = (max(1, round(WIDTH * target_h / HEIGHT)), target_h)
            tiles.append(cv2.cvtColor(np.asarray(image.resize(size, Image.Resampling.NEAREST)), cv2.COLOR_RGBA2BGRA))
        # 飘字单独缓存，不让静态饭碗也出现 +1。
        plus = Image.new("RGBA", (WIDTH, HEIGHT))
        ImageDraw.Draw(plus).text((160, 0), "+1 碗！", font=plus_font, fill=(255, 221, 129, 255), stroke_width=1, stroke_fill=(53, 36, 17, 255))
        self._plus = cv2.cvtColor(np.asarray(plus.resize(size, Image.Resampling.NEAREST)), cv2.COLOR_RGBA2BGRA)
        self._tiles = tiles

    @staticmethod
    def _blend(frame, layer, x, y):
        h, w = layer.shape[:2]
        dst = frame[y:y + h, x:x + w, :3]
        alpha = layer[..., 3:4].astype(np.float32) / 255.0
        dst[:] = (layer[..., :3] * alpha + dst * (1.0 - alpha)).astype(np.uint8)

    def apply(self, frame):
        settings = self.settings()
        if not settings.get("enabled"):
            return
        state = self.counter.state(True)
        height, width = frame.shape[:2]
        target_h = max(48, round(height * 0.16))
        target_h = min(target_h, height, max(1, int(width * HEIGHT / WIDTH)))
        cache_key = state["total"], target_h
        if self._cache_key != cache_key:
            self._render_tiles(*cache_key)
            self._cache_key = cache_key
        age = time.time() - state["celebrated_at"]
        animated = 0 <= age < ANIMATION_SECONDS
        index = int(age * 12) % 8 if animated else 0
        layer = self._tiles[index]
        lh, lw = layer.shape[:2]
        pad = max(0, min(round(height * 0.02), width - lw, height - lh))
        x, y = pad, height - lh - pad
        self._blend(frame, layer, x, y)
        if animated:
            self._blend(frame, self._plus, x, y)
