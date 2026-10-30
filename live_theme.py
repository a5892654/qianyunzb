"""Ocean stream skin, composed before Live2D so chat stays behind the avatar."""
from __future__ import annotations

from collections import deque
from pathlib import Path
import math
import threading
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import config

DEFAULTS = config.DEFAULTS["stream_theme"]
# Pixel coordinates on the 1920 × 1080 broadcast canvas; keep the UI in sync.
GAME_RECT = (466, 150, 1410, 793)
BACKGROUND = Path(config.WEB_DIR) / "assets" / "ocean-background.png"
# Bottom radio bar inside the skin's rounded strip.
RADIO_RECT = (660, 978, 1858, 1040)
RADIO_HINT_W = 240
RADIO_SWITCH_SECONDS = 6.0
# Layout sample for the theme preview: a viewer request that just took over.
DEMO_SONG = {
    "qid": 0, "name": "海风来信", "artist": "潮汐", "by": "海盐汽水", "source": "request",
    "loading": False, "paused": False, "pos": 74.0, "dur": 252.0, "switched": True,
    "levels": [0.55, 0.86, 0.62, 1.0, 0.48, 0.79, 0.66],
}


def _clock(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def validate_settings(patch):
    if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
        raise ValueError("直播主题设置无效")
    if "preset" in patch and patch["preset"] not in ("classic", "ocean"):
        raise ValueError("请选择原始画面或蓝色海洋主题")
    for key, limit in (("title", 24), ("subtitle", 60), ("ticker", 300)):
        if key in patch and (not isinstance(patch[key], str) or len(patch[key]) > limit):
            raise ValueError(f"主题文字过长（{key} 最多 {limit} 字）")
    if "show_chat" in patch and type(patch["show_chat"]) is not bool:
        raise ValueError("弹幕显示开关无效")
    for key, low, high in (("avatar_x", 0, 1), ("avatar_y", 0, 1), ("avatar_size", 0.2, 1.4), ("avatar_zoom", 1, 3)):
        if key in patch:
            value = patch[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("海洋主题皮套位置或大小超出范围")


def _font(size, bold=False):
    for name in (("msyhbd.ttc", "msyh.ttc") if bold else ("msyh.ttc", "segoeui.ttf")):
        path = Path("C:/Windows/Fonts") / name
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


class StreamTheme:
    def __init__(self, settings, song_state=None):
        self.settings = settings
        self.song_state = song_state
        self._lock = threading.Lock()
        self._messages = deque(maxlen=80)
        self._version = 0
        self._skin_key = None
        self._base = None
        self._chat_key = None
        self._chat = None
        self._radio_key = None
        self._radio = None
        self._song_qid = None
        self._switch_at = -math.inf
        self._measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        self._started = time.monotonic()
        self._fonts = {}

    @property
    def active(self):
        return self.settings().get("preset", "classic") == "ocean"

    def font(self, size, bold=False):
        if (size, bold) not in self._fonts:
            self._fonts[size, bold] = _font(size, bold)
        return self._fonts[size, bold]

    def symbol(self, size):
        """雅黑没有 ♪ 这类符号，单独借 Segoe UI Symbol 画音符。"""
        key = ("symbol", size)
        if key not in self._fonts:
            path = Path("C:/Windows/Fonts/seguisym.ttf")
            self._fonts[key] = ImageFont.truetype(str(path), size) if path.is_file() else self.font(size)
        return self._fonts[key]

    def clear(self):
        with self._lock:
            self._messages.clear()
            self._version += 1

    def add_event(self, event):
        kind = event.get("type")
        if kind not in ("danmaku", "superchat", "gift", "guard", "follow"):
            return
        # Respect moderation decisions; paused replies and per-user cooldowns can
        # still appear, but blocked text/users must not be put back on screen.
        if event.get("skip") and any(word in str(event["skip"]) for word in ("屏蔽", "忽略名单", "黑名单")):
            return
        text = " ".join(str(event.get("desc", "")).split())[:320]
        if not text:
            return
        name, sep, body = text.partition("：") if kind in ("danmaku", "superchat") else ("", "", text)
        with self._lock:
            self._messages.append((time.monotonic(), name[:24] if sep else "海面来信", body if sep else text, kind))
            self._version += 1

    def _text(self, draw, xy, text, font, fill, width):
        text = str(text)
        if draw.textlength(text, font=font) > width:
            while text and draw.textlength(text + "…", font=font) > width:
                text = text[:-1]
            text += "…"
        draw.text(xy, text, font=font, fill=fill)

    def _make_skin(self, settings):
        key = (settings.get("title", DEFAULTS["title"]), settings.get("subtitle", DEFAULTS["subtitle"]))
        if key == self._skin_key:
            return
        # Pillow handles non-ASCII Windows paths; avoid cv2.imread here.
        with Image.open(BACKGROUND) as background:
            image = background.convert("RGBA").resize((1920, 1080), Image.Resampling.LANCZOS)
        glass = Image.new("RGBA", image.size)
        d = ImageDraw.Draw(glass)
        d.rounded_rectangle((26, 28, 430, 716), 28, fill=(5, 47, 86, 145), outline=(189, 235, 255, 190), width=2)
        d.rounded_rectangle((452, 136, 1890, 957), 18, fill=(4, 36, 68, 210), outline=(202, 244, 255, 245), width=3)
        d.rounded_rectangle((465, 974, 1878, 1044), 20, fill=(8, 53, 88, 180), outline=(174, 229, 251, 180), width=1)
        d.rounded_rectangle((42, 168, 413, 208), 20, fill=(132, 219, 246, 40), outline=(179, 233, 253, 120))
        d.rounded_rectangle((452, 66, 935, 118), 20, fill=(5, 47, 86, 165), outline=(189, 235, 255, 120))
        image = Image.alpha_composite(image, glass)
        d = ImageDraw.Draw(image)
        self._text(d, (48, 49), key[0], self.font(32, True), "#f1fcff", 357)
        self._text(d, (48, 105), key[1], self.font(16), "#c5eaf8", 354)
        d.text((64, 177), "海 面 来 信   /   CHAT", font=self.font(15), fill="#ddf8ff")
        d.text((470, 80), "NOW PLAYING", font=self.font(17, True), fill="#ecfcff")
        d.text((627, 80), "／  此刻，冒险正在发生", font=self.font(17), fill="#d5f2ff")
        d.rounded_rectangle((1672, 69, 1876, 112), 21, fill="#e3f8ff")
        d.ellipse((1690, 86, 1700, 96), fill="#198ba6")
        d.text((1713, 79), "OCEAN ROOM", font=self.font(16, True), fill="#21617b")
        d.text((488, 995), "深 海 电 台", font=self.font(19, True), fill="#c6f0ff")
        d.line((638, 991, 638, 1027), fill="#92cee7", width=1)
        # Fine white registration details frame the game without covering it.
        for x, y, sx, sy in ((452, 136, 1, 1), (1890, 136, -1, 1), (452, 957, 1, -1), (1890, 957, -1, -1)):
            d.line(((x, y + sy * 28), (x, y), (x + sx * 28, y)), fill="#efffff", width=4)
        self._base = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGBA2BGRA)
        self._skin_key = key

    def _chat_panel(self, settings, now):
        with self._lock:
            messages = [m for m in self._messages if now - m[0] < 120][-4:]
            key = (self._version, len(messages), settings.get("show_chat", True))
        if key == self._chat_key:
            return self._chat
        image = Image.fromarray(cv2.cvtColor(self._base[221:700, 42:414], cv2.COLOR_BGRA2RGBA))
        d = ImageDraw.Draw(image)
        if settings.get("show_chat", True):
            if not messages:
                d.text((22, 25), "等一封来自你的海面来信…", font=self.font(18), fill="#d2f1ff")
                d.text((22, 59), "弹幕将在这里实时出现", font=self.font(16), fill="#acd9ee")
            for i, (_, name, text, kind) in enumerate(messages):
                y = i * 111
                special = kind in ("gift", "guard", "superchat")
                d.rounded_rectangle((0, y, 371, y + 99), 14, fill=(221, 247, 255, 239) if special else (233, 249, 255, 227), outline="#a7def3", width=1)
                d.ellipse((16, y + 17, 24, y + 25), fill="#24a5ca")
                self._text(d, (34, y + 9), name, self.font(16, True), "#36728d", 317)
                # Wrap to two lines using measured glyph widths (CJK + emoji).
                line, lines = "", []
                for char in text:
                    if d.textlength(line + char, font=self.font(20)) > 332:
                        lines.append(line)
                        line = ""
                        if len(lines) == 2:
                            break
                    line += char
                if len(lines) < 2 and line:
                    lines.append(line)
                elif len(lines) == 2:
                    lines[-1] = lines[-1][:-1] + "…"
                for j, line in enumerate(lines):
                    d.text((17, y + 36 + j * 27), line, font=self.font(20), fill="#194a66")
        self._chat = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGBA2BGRA)
        self._chat_key = key
        return self._chat

    def _radio_strip(self, settings, now):
        state = None
        if self.song_state is not None:
            try:
                state = self.song_state()
            except Exception:
                state = None
        song = (state or {}).get("song")
        enabled = (state or {}).get("enabled", True)
        if song:
            qid = song.get("qid")
            if self._song_qid is None:
                self._song_qid = qid
                if song.get("switched"):
                    self._switch_at = now
            elif qid != self._song_qid:
                self._song_qid = qid
                if song.get("source") != "autoplay":
                    self._switch_at = now
        switched = bool(song) and song.get("source") != "autoplay" and now - self._switch_at < RADIO_SWITCH_SECONDS
        text = settings.get("ticker", DEFAULTS["ticker"]).strip() if not song else ""
        width = RADIO_RECT[2] - RADIO_RECT[0] - (RADIO_HINT_W + 18 if enabled else 0)
        tick = 0
        if text and self._measure.textlength(text, font=self.font(20)) > width:
            tick = int((now - self._started) * 10)
        if song:
            sig = (song.get("qid"), song.get("name"), song.get("artist"), song.get("by"), song.get("source"),
                   bool(song.get("loading")), bool(song.get("paused")),
                   int(song.get("pos") or 0), int(song.get("dur") or 0))
        else:
            sig = None
        beat = int(now * 15) if song and song.get("levels") else 0
        key = (sig, text, tick, enabled, switched, beat)
        if key != self._radio_key:
            x0, y0, x1, y1 = RADIO_RECT
            image = Image.fromarray(cv2.cvtColor(self._base[y0:y1, x0:x1], cv2.COLOR_BGRA2RGBA))
            self._draw_radio(image, song, enabled, tick, text, switched)
            self._radio = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGBA2BGRA)
            self._radio_key = key
        return self._radio

    def _draw_radio(self, image, song, enabled, tick, text, switched):
        d = ImageDraw.Draw(image)
        center = image.width - (RADIO_HINT_W + 18 if enabled else 0)
        if song:
            title = f"正在播放《{song.get('name') or '未知歌曲'}》"
            artist = str(song.get("artist") or "").strip()
            if song.get("loading"):
                title = f"正在准备《{song.get('name') or '未知歌曲'}》"
            elif song.get("paused"):
                title = f"已暂停《{song.get('name') or '未知歌曲'}》"
            if artist:
                title += f" · {artist}"
            note_font = self.symbol(18)
            d.text((0, 4), "♪", font=note_font, fill="#c9f6ff")
            offset = int(d.textlength("♪", font=note_font)) + 8
            self._text(d, (offset, 2), title, self.font(20, True), "#f1fcff", center - offset)
            pos, dur = float(song.get("pos") or 0), float(song.get("dur") or 0)
            by = str(song.get("by") or "").strip()
            if song.get("loading"):
                note, color = (f"{by} 的点歌马上开始" if by and by != "歌单自动播放" else "电台曲目准备中…"), "#a9d9ec"
            elif song.get("paused"):
                note, color = "点歌台已暂停 · 发「点歌+歌名」切换电台", "#a9d9ec"
            elif switched and by:
                note, color = f"已切换电台 · 由 {by} 点播", "#a9f0dd"
            elif song.get("source") == "autoplay":
                note, color = "深海电台循环中 · 发「点歌+歌名」换你点的歌", "#a9d9ec"
            else:
                note, color = (f"由 {by} 点播 · 发「点歌+歌名」随时切换电台" if by
                               else "发「点歌+歌名」随时切换电台"), "#a9d9ec"
            time_text = f"{_clock(pos)} / {_clock(dur)}" if dur > 0 else ""
            self._text(d, (0, 31), note, self.font(14), color, max(120, center - (96 if time_text else 0)))
            if time_text:
                d.text((center, 33), time_text, font=self.font(12), fill="#a6d7e8", anchor="ra")
            if not song.get("loading") and dur > 0:
                d.rounded_rectangle((0, 54, center, 58), 2, fill=(255, 255, 255, 60))
                if pos > 0:
                    d.rounded_rectangle((0, 54, max(4, int(center * min(1.0, pos / dur))), 58), 2, fill="#86e6ff")
        else:
            if enabled:
                self._text(d, (0, 33), "发弹幕「点歌+歌名」，切换到你点的歌", self.font(14), "#a9d9ec", center)
            if text:
                length = max(1, int(d.textlength(text, font=self.font(20))))
                if length <= center:
                    d.text((0, 4), text, font=self.font(20), fill="#effcff")
                else:
                    x = -(tick * 4 % (length + 100))
                    d.text((x, 4), text, font=self.font(20), fill="#effcff")
                    d.text((x + length + 100, 4), text, font=self.font(20), fill="#effcff")
        if enabled:
            note, label = "♪", "发送「点歌+歌名」"
            f_note, f_label = self.symbol(17), self.font(17, True)
            note_w = d.textlength(note, font=f_note)
            offset = image.width - 8 - note_w - 6 - d.textlength(label, font=f_label)
            cy = image.height / 2
            if song and song.get("levels"):
                self._draw_equalizer(d, song["levels"], offset - 14, cy)
            d.text((offset, cy), note, font=f_note, fill="#c9f6ff", anchor="lm")
            d.text((offset + note_w + 6, cy), label, font=f_label, fill="#f1fcff", anchor="lm")

    def _draw_equalizer(self, d, levels, right, cy):
        levels = levels[:9]
        bar_w, gap = 4, 3
        x = right - (len(levels) * bar_w + (len(levels) - 1) * gap)
        for i, level in enumerate(levels):
            height = max(6, int(34 * min(1.0, float(level))))
            left = x + i * (bar_w + gap)
            d.rounded_rectangle((left, cy + 17 - height, left + bar_w, cy + 17), 2, fill="#a9efff")

    def compose(self, source):
        settings = self.settings()
        if settings.get("preset", "classic") != "ocean":
            return None
        self._make_skin(settings)
        now = time.monotonic()
        canvas = self._base.copy()
        canvas[221:700, 42:414] = self._chat_panel(settings, now)
        x0, y0, x1, y1 = RADIO_RECT
        canvas[y0:y1, x0:x1] = self._radio_strip(settings, now)
        # Contain the original capture: preserve the entire game HUD, including
        # games with 4:3, ultrawide and portrait client areas.
        x, y, w, h = GAME_RECT
        sh, sw = source.shape[:2]
        scale = min(w / sw, h / sh)
        tw, th = max(1, round(sw * scale)), max(1, round(sh * scale))
        game = cv2.resize(source, (tw, th), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        canvas[y:y + h, x:x + w] = (42, 25, 13, 255)
        ox, oy = x + (w - tw) // 2, y + (h - th) // 2
        canvas[oy:oy + th, ox:ox + tw] = game
        canvas[..., 3] = 255
        return canvas


def preview_jpeg(settings):
    """Render a local layout sample without starting capture or a broadcast."""
    renderer = StreamTheme(lambda: settings, lambda: {"enabled": True, "song": dict(DEMO_SONG)})
    game = Image.new("RGBA", (1920, 1080), "#10273a")
    draw = ImageDraw.Draw(game)
    for x in range(0, 1920, 80):
        draw.line((x, 0, x, 1080), fill="#153347")
    for y in range(0, 1080, 80):
        draw.line((0, y, 1920, y), fill="#153347")
    draw.text((960, 470), "GAMEPLAY", font=renderer.font(52, True), fill="#a9d5e5", anchor="mm")
    draw.text((960, 555), "游戏画面显示区域", font=renderer.font(32), fill="#7399ad", anchor="mm")
    draw.text((960, 615), "启动游戏预览后，显示实际采集画面", font=renderer.font(22), fill="#7399ad", anchor="mm")
    source = cv2.cvtColor(np.asarray(game), cv2.COLOR_RGBA2BGRA)
    canvas = renderer.compose(source)
    if canvas is None:
        canvas = source
    ok, encoded = cv2.imencode(".jpg", canvas[..., :3], [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError("主题预览生成失败")
    return encoded.tobytes()


class ThemeAvatar:
    """A theme-local upper-body crop; the underlying Live2D settings stay intact."""
    def __init__(self, overlay, theme):
        self.overlay, self.theme = overlay, theme

    def frame(self):
        layer = self.overlay.frame()
        if layer is None or not self.theme.active:
            return layer
        zoom = float(self.theme.settings().get("avatar_zoom", DEFAULTS["avatar_zoom"]))
        zoom = max(1.0, min(3.0, zoom))
        h, w = layer.shape[:2]
        ch, cw = max(1, round(h / zoom)), max(1, round(w / zoom))
        x, y = (w - cw) // 2, min(h - ch, round(h * 0.065))
        return layer[y:y + ch, x:x + cw]
