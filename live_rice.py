"""直播白饭计数与透明帧合成；不会向游戏写入任何状态。"""
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import config

ANIMATION_SECONDS = 2.4
WIDTH, HEIGHT = 360, 168
GIFT_SENDS_PER_BOWL = 10
DEFAULT_LARGE_GIFT_YUAN = 10


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) - {"enabled", "large_gift_yuan", "overlay_size", "overlay_x", "overlay_y"} or ("enabled" in settings and type(settings["enabled"]) is not bool):
        raise ValueError("白饭计数开关无效")
    for key, low, high in (("overlay_size", 0.04, 0.6), ("overlay_x", 0, 1), ("overlay_y", 0, 1)):
        if key not in settings or (key != "overlay_size" and settings[key] is None):
            continue
        number = settings[key]
        if type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError("白饭位置或大小超出范围，请重新调整")
    value = settings.get("large_gift_yuan", DEFAULT_LARGE_GIFT_YUAN)
    if type(value) not in (int, float):
        raise ValueError("大额礼物门槛需为至少 0.01 元的有效金额")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < Decimal("0.01"):
        raise ValueError("大额礼物门槛需为至少 0.01 元的有效金额")


def layout_rect(settings, width, height):
    """Return the visible rice widget rectangle in output pixels."""
    target_h = min(max(1, round(height * settings.get("overlay_size", 0.16))),
                   height, max(1, int(width * HEIGHT / WIDTH)))
    target_w = min(width, max(1, round(WIDTH * target_h / HEIGHT)))
    pad = max(0, min(round(height * 0.02), width - target_w, height - target_h))
    cx, cy = settings.get("overlay_x"), settings.get("overlay_y")
    x = pad if cx is None else round(width * cx - target_w / 2)
    y = height - target_h - pad if cy is None else round(height * cy - target_h / 2)
    return max(0, min(x, width - target_w)), max(0, min(y, height - target_h)), target_w, target_h


class RiceCounter:
    def __init__(self, path=None):
        self.path = Path(path or os.path.join(config.BASE_DIR, "rice_counter.json"))
        self.lock = threading.RLock()
        self.total = 0
        self.gift_progress = 0
        self.seen = set()
        self.session = uuid.uuid4().hex
        self.event_id = ""
        self.celebrated_at = 0.0
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.total = max(0, int(data.get("total", 0)))
            self.gift_progress = max(0, int(data.get("gift_progress", 0))) % GIFT_SENDS_PER_BOWL
            self.seen = set(data.get("seen", []))
            self.session = str(data.get("session") or self.session)

    def _save(self, total, seen, session, gift_progress=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        progress = self.gift_progress if gift_progress is None else gift_progress
        tmp.write_text(json.dumps({"total": total, "seen": sorted(seen), "session": session,
                                   "gift_progress": progress}, ensure_ascii=False), encoding="utf-8")
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

    def record_gift(self, event, room_id=None, large_gift_yuan=DEFAULT_LARGE_GIFT_YUAN):
        """普通送礼十次加一碗，大额单独加一碗并保留普通进度。"""
        if event.get("type") != "gift" or event.get("test"):
            return False
        quantity = event.get("num")
        if isinstance(quantity, bool):
            return False
        try:
            if int(quantity) <= 0:
                return False
        except (TypeError, ValueError, OverflowError):
            return False
        identity = next(((field, str(event[field])) for field in ("gift_tid", "gift_rnd")
                         if str(event.get(field) or "").strip() not in ("", "0")), None)
        if identity is None:
            # 没有事务ID时只对同一事件对象去重，不能吞掉同一秒内的独立送礼。
            identity = ("event", event.setdefault("_rice_gift_id", uuid.uuid4().hex))
        payload = [str(event.get("room_id") or room_id or ""), str(event.get("uid") or ""), *identity]
        key = hashlib.sha256(("gift:" + json.dumps(payload, ensure_ascii=False)).encode()).hexdigest()
        validate_settings({"large_gift_yuan": large_gift_yuan})
        try:
            amount = Decimal(str(event.get("value", 0)))
        except InvalidOperation:
            amount = Decimal(0)
        large = event.get("paid") is True and amount.is_finite() and amount >= Decimal(str(large_gift_yuan))
        with self.lock:
            if key in self.seen:
                return False
            progress = self.gift_progress if large else (self.gift_progress + 1) % GIFT_SENDS_PER_BOWL
            gained = large or progress == 0
            seen = self.seen | {key}
            self._save(self.total + int(gained), seen, self.session, progress)
            self.total += int(gained)
            self.gift_progress = progress
            self.seen = seen
            if gained:
                self.preview()
            return gained

    def preview(self):
        """试播动画不计入真实战绩，也不写入累计文件。"""
        with self.lock:
            self.event_id = uuid.uuid4().hex
            self.celebrated_at = time.time()

    def state(self, enabled=False):
        with self.lock:
            return {"enabled": bool(enabled), "total": self.total, "event_id": self.event_id,
                    "celebrated_at": self.celebrated_at, "gift_progress": self.gift_progress}


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
        plus_font = ImageFont.truetype(font_path, 23)
        cw, ch = self._sheet.width // 4, self._sheet.height // 2
        tiles = []
        for i in range(8):
            image = Image.new("RGBA", (WIDTH, HEIGHT))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((0, 25, WIDTH - 1, HEIGHT - 1), radius=14, fill=(22, 25, 36, 228), outline=(206, 175, 120, 210), width=2)
            bowl = self._sheet.crop(((i % 4) * cw, (i // 4) * ch, (i % 4 + 1) * cw, (i // 4 + 1) * ch))
            image.alpha_composite(bowl.resize((134, 134), Image.Resampling.NEAREST), (3, 29))
            draw.text((145, 54), "大碗白饭", font=title_font, fill=(231, 212, 176, 255))
            draw.text((142, 78), f"{count} 碗", font=number_font, fill=(255, 245, 216, 255))
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
        x, y, _, target_h = layout_rect(settings, width, height)
        cache_key = state["total"], target_h
        if self._cache_key != cache_key:
            self._render_tiles(*cache_key)
            self._cache_key = cache_key
        age = time.time() - state["celebrated_at"]
        animated = 0 <= age < ANIMATION_SECONDS
        index = int(age * 12) % 8 if animated else 0
        layer = self._tiles[index]
        self._blend(frame, layer, x, y)
        if animated:
            self._blend(frame, self._plus, x, y)
