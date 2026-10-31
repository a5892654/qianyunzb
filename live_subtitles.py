"""Cached bottom subtitles for the native source; follows /overlay events."""
from __future__ import annotations

import os
import threading
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


class SourceSubtitles:
    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._text = ""
        self._hide_at = None
        self._key = None
        self._layer = None
        self._font = None

    def event(self, kind, data):
        if kind not in ("speak", "speak_end", "overlay_config", "bye"):
            return
        with self._lock:
            if kind == "bye" or (kind == "overlay_config" and not data.get("enabled", True)):
                self._text, self._hide_at = "", None
            elif self.settings().get("enabled", True):
                if kind == "speak":
                    self._text, self._hide_at = str(data.get("text", "")), None
                elif kind == "speak_end":
                    self._hide_at = time.monotonic() + float(self.settings().get("seconds", 8))

    def _render(self, text, width):
        if self._font is None:
            for name in ("msyhbd.ttc", "msyh.ttc", "segoeuib.ttf"):
                try:
                    self._font = ImageFont.truetype(os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                                               "Fonts", name), 34)
                    break
                except OSError:
                    continue
            if self._font is None:
                self._font = ImageFont.load_default(size=34)
        font = self._font
        max_width = max(60, int(width * 0.92) - 56)
        lines, line = [], ""
        for char in text:
            if char == "\n" or (line and font.getlength(line + char) > max_width):
                lines.append(line)
                line = ""
            if char != "\n":
                line += char
        lines.append(line)
        text = "\n".join(lines)
        text_width = max((int(font.getlength(value)) for value in lines), default=0)
        image = Image.new("RGBA", (min(width, text_width + 56), 32 + len(lines) * 46))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, image.width - 1, image.height - 1), 16,
                               fill=(20, 22, 32, 199), outline=(251, 114, 153, 255), width=2)
        draw.multiline_text((28, 12), text, font=font, fill="white", spacing=5)
        layer = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGBA2BGRA)
        self._alpha = layer[..., 3:4].astype(np.float32) / 255
        self._color = layer[..., :3].astype(np.float32) * self._alpha
        self._inverse = 1 - self._alpha
        self._layer = layer

    def apply(self, frame):
        if not self.settings().get("enabled", True):
            return False
        with self._lock:
            text, hide_at = self._text, self._hide_at
        if not text or (hide_at is not None and time.monotonic() >= hide_at):
            return False
        height, width = frame.shape[:2]
        key = text, width
        if key != self._key:
            self._render(*key)
            self._key = key
        lh, lw = self._layer.shape[:2]
        visible_h = min(lh, max(0, height - 16))
        if not visible_h:
            return False
        dst = frame[height - 16 - visible_h:height - 16, (width - lw) // 2:(width + lw) // 2, :3]
        dst[:] = (self._color[-visible_h:] + dst * self._inverse[-visible_h:]).astype(np.uint8)
        return True
