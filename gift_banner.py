"""送礼横幅：全屏飘过 3 秒，让没送过的人看见这里有人送。"""
from __future__ import annotations

import collections
import functools
import math
import os
import threading
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

DURATION = 3.0
IN_SECONDS = 0.4
OUT_SECONDS = 0.55
QUEUE_LIMIT = 4
STALE_SECONDS = 15.0
MAX_NAME, MAX_GIFT = 12, 16
REFERENCE_HEIGHT = 1080
_FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


@functools.lru_cache(maxsize=24)
def _font(size, bold=False):
    for name in (("msyhbd.ttc", "msyh.ttc") if bold else ("msyh.ttc", "segoeui.ttf")):
        path = os.path.join(_FONT_DIR, name)
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _ease_out(t):
    return 1 - (1 - t) ** 3


def _ease_in(t):
    return t ** 3


def _blend(frame, layer, x, y):
    """把 BGRA 横幅裁到画面内再合成，避免负坐标或超出边界。"""
    height, width = layer.shape[:2]
    frame_h, frame_w = frame.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(frame_w, x + width), min(frame_h, y + height)
    if x1 <= x0 or y1 <= y0:
        return
    source = layer[y0 - y:y1 - y, x0 - x:x1 - x]
    target = frame[y0:y1, x0:x1]
    alpha = source[..., 3:4].astype(np.float32) * (1.0 / 255.0)
    target[..., :3] = (source[..., :3] * alpha + target[..., :3] * (1.0 - alpha)).astype(np.uint8)


class GiftBanner:
    def __init__(self):
        self._lock = threading.RLock()
        self._queue = collections.deque()
        self._current = None
        self._tiles = {}
        self._error = ""

    @property
    def error(self):
        with self._lock:
            return self._error

    def trigger(self, ev):
        if ev.get("type") != "gift":
            return False
        name = " ".join(str(ev.get("name") or "").split())[:MAX_NAME] or "观众"
        gift = " ".join(str(ev.get("gift") or "").split())[:MAX_GIFT] or "礼物"
        num = ev.get("num")
        if isinstance(num, bool):
            num = 1
        try:
            num = max(1, int(num or 1))
        except (TypeError, ValueError, OverflowError):
            num = 1
        now = time.monotonic()
        with self._lock:
            last = self._queue[-1] if self._queue else None
            if (last and last["name"] == name and last["gift"] == gift
                    and now - last["at"] < DURATION):
                last["num"] += num
                return True
            if len(self._queue) >= QUEUE_LIMIT:
                self._queue.popleft()
            self._queue.append({"name": name, "gift": gift, "num": num, "at": now})
        return True

    def apply(self, frame):
        try:
            picked = self._pick(time.monotonic())
            if picked is None:
                return False
            item, elapsed = picked
            layer, target_x = self._tile(item, frame.shape[1], frame.shape[0])
            height = layer.shape[0]
            width = frame.shape[1]
            x = target_x
            if elapsed < IN_SECONDS:
                progress = _ease_out(elapsed / IN_SECONDS)
                x = round(width + 16 + (target_x - width - 16) * progress)
            elif elapsed > DURATION - OUT_SECONDS:
                progress = _ease_in((elapsed - (DURATION - OUT_SECONDS)) / OUT_SECONDS)
                x = round(target_x + (-layer.shape[1] - 16 - target_x) * progress)
            y = round((frame.shape[0] - height) * 0.30)
            _blend(frame, layer, x, y)
            if self._error:
                with self._lock:
                    self._error = ""
            return True
        except Exception as exc:  # noqa: BLE001 - 合成失败不能拖垮整帧
            with self._lock:
                self._error = f"送礼横幅合成失败：{exc}"
                self._current = None
            return False

    # ---------------- 内部 ----------------

    def _pick(self, now):
        with self._lock:
            if self._current and now - self._current["started"] >= DURATION:
                self._current = None
            if self._current is None:
                while self._queue and now - self._queue[0]["at"] > STALE_SECONDS:
                    self._queue.popleft()
                if self._queue:
                    item = self._queue.popleft()
                    item["started"] = now
                    self._current = item
            current = self._current
        return (current, now - current["started"]) if current else None

    def _tile(self, item, frame_w, frame_h):
        key = (item["name"], item["gift"], item["num"], frame_w, frame_h)
        cached = self._tiles.get(key)
        if cached is not None:
            return cached
        scale = frame_h / REFERENCE_HEIGHT
        size = max(16, round(56 * scale))
        pad = max(12, round(54 * scale))
        radius = max(8, round(28 * scale))
        border = max(1, round(2 * scale))
        max_width = max(160, round(frame_w * 0.9))
        count = f" ×{item['num']}" if item["num"] > 1 else ""
        parts = [("@", (166, 197, 218, 255), True),
                 (item["name"], (255, 219, 138, 255), True),
                 (" 送出了 ", (216, 234, 247, 255), False),
                 (item["gift"], (255, 255, 255, 255), True),
                 (count, (255, 219, 138, 255), True)]
        measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

        def text_width(font_size):
            return sum(measure.textlength(text, font=_font(font_size, bold))
                       for text, _, bold in parts)

        while size > 20 and text_width(size) + pad * 2 > max_width:
            size = max(20, int(size * 0.9))
        card_w = min(max_width, int(math.ceil(text_width(size))) + pad * 2)
        card_h = max(round(118 * scale), round(size * 2.05))
        image = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, round(6 * scale), card_w - 1, card_h - 1), radius,
                               fill=(8, 24, 42, 214), outline=(255, 207, 120, 235), width=border)
        draw.rounded_rectangle((round(14 * scale), round(24 * scale),
                                round(14 * scale) + max(2, round(4 * scale)), card_h - round(24 * scale)),
                               max(1, round(2 * scale)), fill=(255, 207, 120, 225))
        draw.line((radius, round(7 * scale), card_w - radius, round(7 * scale)),
                  fill=(255, 255, 255, 30), width=max(1, border))
        x = round(14 * scale) + max(2, round(4 * scale)) + round(22 * scale)
        baseline = (card_h + round(6 * scale)) / 2
        for text, color, bold in parts:
            font = _font(size, bold)
            draw.text((x, baseline), text, font=font, fill=color, anchor="lm")
            x += draw.textlength(text, font=font)
        layer = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGBA2BGRA)
        if len(self._tiles) >= 8:
            self._tiles.clear()
        self._tiles[key] = (layer, max(0, (frame_w - card_w) // 2))
        return self._tiles[key]
