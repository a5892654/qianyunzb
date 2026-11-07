"""底部滚动横幅：常驻在直播画面最底部的跑马灯。"""
from __future__ import annotations

import functools
import math
import os
import threading
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

MAX_TEXT = 120
REFERENCE_HEIGHT = 1080
BAR_HEIGHT = 36
FONT_SIZE = 23
SPEED = 62.0  # 像素/秒，一条文字走完画面约半分钟
LEAD_SECONDS = 12.0  # 启动时让文字已在画面里，空白预览也看得到内容
_FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
BAR_FILL = (4, 30, 54, 196)
BAR_EDGE = (138, 227, 255, 150)
TEXT_FILL = (241, 252, 255, 255)


def validate_settings(patch):
    if not isinstance(patch, dict) or set(patch) - {"enabled", "text"}:
        raise ValueError("底部滚动横幅设置无效")
    if "enabled" in patch and type(patch["enabled"]) is not bool:
        raise ValueError("底部滚动横幅开关无效")
    if "text" in patch and (not isinstance(patch["text"], str) or len(patch["text"]) > MAX_TEXT):
        raise ValueError(f"底部滚动横幅文字最多 {MAX_TEXT} 字")


@functools.lru_cache(maxsize=12)
def _font(size, bold=True):
    names = ("msyhbd.ttc", "msyh.ttc", "segoeuib.ttf") if bold else ("msyh.ttc", "segoeui.ttf")
    for name in names:
        path = os.path.join(_FONT_DIR, name)
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _premultiply(layer):
    alpha = layer[..., 3:4].astype(np.float32) / 255.0
    return layer[..., :3].astype(np.float32) * alpha, 1.0 - alpha


class MarqueeBanner:
    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._bar_key = None
        self._bar_color = None
        self._bar_inverse = None
        self._layer_key = None
        self._layer_width = 0
        self._layer_color = None
        self._layer_inverse = None
        self._layer_static = False
        self._started = time.monotonic() - LEAD_SECONDS

    def apply(self, frame):
        settings = self.settings() or {}
        if not settings.get("enabled", True):
            return False
        text = " ".join(str(settings.get("text") or "").split())
        if not text:
            return False
        height, width = frame.shape[:2]
        scale = height / REFERENCE_HEIGHT
        bar_h = max(16, round(BAR_HEIGHT * scale))
        if bar_h >= height:
            return False
        top = height - bar_h
        bar_color, bar_inverse = self._background(width, bar_h, scale)
        dst = frame[top:height, :, :3]
        dst[:] = (bar_color + dst * bar_inverse).astype(np.uint8)
        color, inverse, layer_w, static = self._text_layer(text, width, bar_h, scale)
        if static:
            self._paste(frame, color, inverse, layer_w, bar_h, (width - layer_w) // 2, top, width)
            return True
        # 初相位让文字从右侧入场，而不是一上来就在左边往外走。
        offset = (int((time.monotonic() - self._started) * SPEED * scale) + layer_w - width) % layer_w
        x = -offset
        while x < width:
            self._paste(frame, color, inverse, layer_w, bar_h, x, top, width)
            x += layer_w
        return True

    def _background(self, width, bar_h, scale):
        key = (width, bar_h)
        with self._lock:
            if key == self._bar_key:
                return self._bar_color, self._bar_inverse
        image = Image.new("RGBA", (width, bar_h), BAR_FILL)
        draw = ImageDraw.Draw(image)
        edge = max(1, round(2 * scale))
        draw.rectangle((0, 0, width - 1, edge - 1), fill=BAR_EDGE)
        color, inverse = _premultiply(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGBA2BGRA))
        with self._lock:
            self._bar_key = key
            self._bar_color, self._bar_inverse = color, inverse
        return color, inverse

    def _text_layer(self, text, width, bar_h, scale):
        key = (text, width, bar_h)
        with self._lock:
            if key == self._layer_key:
                return self._layer_color, self._layer_inverse, self._layer_width, self._layer_static
        size = max(12, round(FONT_SIZE * scale))
        font = _font(size)
        measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        text_w = int(math.ceil(measure.textlength(text, font=font)))
        pad = max(10, round(22 * scale))
        static = text_w + pad * 2 <= width
        if static:
            layer_w = text_w + pad * 2
            image = Image.new("RGBA", (layer_w, bar_h), (0, 0, 0, 0))
            ImageDraw.Draw(image).text((layer_w / 2, bar_h / 2), text, font=font, fill=TEXT_FILL, anchor="mm")
        else:
            # 文字后留一整屏空白，保证任何时刻画面里只有一条，不会平铺成好几份。
            layer_w = max(1, text_w + width)
            image = Image.new("RGBA", (layer_w, bar_h), (0, 0, 0, 0))
            ImageDraw.Draw(image).text((0, bar_h / 2), text, font=font, fill=TEXT_FILL, anchor="lm")
        color, inverse = _premultiply(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGBA2BGRA))
        with self._lock:
            self._layer_key = key
            self._layer_width, self._layer_static = layer_w, static
            self._layer_color, self._layer_inverse = color, inverse
        return color, inverse, layer_w, static

    @staticmethod
    def _paste(frame, color, inverse, seg_w, bar_h, x, y, frame_w):
        x0, x1 = max(0, x), min(frame_w, x + seg_w)
        if x1 <= x0:
            return
        sx0 = x0 - x
        dst = frame[y:y + bar_h, x0:x1, :3]
        dst[:] = (color[:, sx0:sx0 + x1 - x0] + dst * inverse[:, sx0:sx0 + x1 - x0]).astype(np.uint8)
