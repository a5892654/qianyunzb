"""上班摸鱼节目画面：工资、倒计时、代码工位、观众原话和鲨总查岗。"""
from __future__ import annotations

import math
import bisect
import logging
import threading
import time
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from config import WEB_DIR
from wage_slack import SLACK_BY_ID

W, H = 1920, 1080
_WHITE = (244, 250, 255)
_GREEN = (70, 232, 142)
_YELLOW = (255, 224, 93)
_RED = (255, 90, 126)
_BLUE = (173, 221, 255)
_BOSS_HEIGHT = 1080
_BOSS_TOP = 280
log = logging.getLogger('live_wage')
_CODE_LINES = (
    '# late_penalty.py — 迟到扣钱规则',
    '# 今天也要准时下班！',
    'from dataclasses import dataclass',
    '',
    '@dataclass',
    'class LateRecord:',
    '    name: str',
    '    minutes: int  # 迟到分钟数',
    '',
    'def salary(record):',
    '    # 鲨总：摸鱼就会白干',
    '    return 6000 - record.minutes * 100',
)


def _font_path(names):
    for name in names:
        path = Path('C:/Windows/Fonts') / name
        if path.is_file():
            return str(path)
    return None


@lru_cache(maxsize=3)
def _boss_animation(pose):
    """GIF 只解码一次，直播线程按帧时长读取透明图层。"""
    folder = Path(WEB_DIR) / 'assets'
    files = {'scold': 'shark-boss-fullbody', 'idle': 'shark-boss-fullbody-idle',
             'inspect': 'shark-boss-inspect', 'assign': 'shark-boss-assign',
             'angry': 'shark-boss-angry', 'approve': 'shark-boss-approve'}
    path = folder / (files.get(pose, 'shark-boss-fullbody-idle') + '.gif')
    if not path.is_file():
        path = folder / 'shark-boss-fullbody.png'
    frames, ends, total = [], [], 0.0
    try:
        with Image.open(path) as source:
            for index in range(min(120, getattr(source, 'n_frames', 1))):
                source.seek(index)
                frames.append(source.convert('RGBA'))
                total += max(.02, float(source.info.get('duration', 100)) / 1000)
                ends.append(total)
    except (OSError, ValueError) as error:
        log.warning('鲨总动画读取失败：%s', error)
    # 全身素材保留；按整段动画的可见边界取景，透明留白不参与人物缩放。
    # 同一段共用边界，避免呼吸或指手时角色忽大忽小。
    boxes = [frame.getchannel('A').getbbox() for frame in frames]
    boxes = [box for box in boxes if box]
    if boxes:
        bounds = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                  max(b[2] for b in boxes), max(b[3] for b in boxes))
        frames = [frame.crop(bounds) for frame in frames]
    return tuple(frames), tuple(ends), total


@lru_cache(maxsize=48)
def _boss_frame(pose, index, height):
    """只放大当前需要的帧，限制缓存，避免为每套 GIF 常驻整段大图。"""
    source = _boss_animation(pose)[0][index]
    width = max(1, round(source.width * height / source.height))
    return Image.fromarray(cv2.resize(np.asarray(source), (width, height), interpolation=cv2.INTER_CUBIC))


class WageRenderer:
    def __init__(self, room_id=0):
        self.room_id = str(room_id or '')
        self._fonts = {}
        self._lock = threading.RLock()
        self._measure = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
        self._speech_cache = None
        self._boss_mode = None
        self._boss_elapsed = 0.0
        self._boss_last_time = 0.0
        self._base = self._make_base()
        self._desk = self._make_desk()
        self._desk_bgra = cv2.cvtColor(np.asarray(self._desk), cv2.COLOR_RGBA2BGRA)
        self._keyboard = self._make_keyboard()
        self._chair = self._make_chair()
        self._props = self._make_props()
        bounds = self._props.getbbox()
        self._props = self._props.crop(bounds)
        self._props_xy = (bounds[0], 900 + bounds[1])

    def font(self, size, bold=False):
        key = (size, bold)
        if key not in self._fonts:
            path = _font_path(('msyhbd.ttc', 'msyh.ttc') if bold else ('msyh.ttc', 'segoeui.ttf'))
            self._fonts[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)
        return self._fonts[key]

    def _make_base(self):
        yy, xx = np.mgrid[:H, :W].astype(np.float32)
        glow = np.exp(-(((xx - 1220) / 1000) ** 2 + ((yy - 550) / 850) ** 2))
        colors = np.array([10, 39, 83]) + glow[..., None] * np.array([22, 55, 75])
        image = Image.fromarray(np.uint8(colors)).convert('RGBA')
        background = Path(WEB_DIR) / 'assets' / 'wage-office-premium.png'
        if background.is_file():
            with Image.open(background) as source:
                image = ImageOps.fit(source.convert('RGBA'), (W, H), method=Image.Resampling.LANCZOS)
        dust = Image.new('RGBA', (W, H))
        d = ImageDraw.Draw(dust)
        # 细小浮尘保留空气感，不盖住背景的灯光与材质。
        for i in range(28):
            x, y = (i * 347 + 35) % W, (i * 167 + 84) % 890
            radius = 1 + i % 3
            d.ellipse((x, y, x + radius * 2, y + radius * 2), fill=(192, 228, 245, 22 + i % 4 * 5))
        d.rounded_rectangle((40, 42, 1880, 166), 26, fill=(0, 7, 20, 120))
        d.rounded_rectangle((45, 552, 754, 872), 14, fill=(0, 6, 19, 155))
        d.rounded_rectangle((40, 169, 612, 213), 12, fill=(6, 23, 44, 195))
        d.rounded_rectangle((1644, 169, 1881, 213), 12, fill=(6, 23, 44, 195))
        image = Image.alpha_composite(image, dust.filter(ImageFilter.GaussianBlur(1.5)))
        d = ImageDraw.Draw(image)
        d.rounded_rectangle((40, 32, 1880, 154), 24, fill=(8, 31, 62, 230),
                            outline=(126, 190, 218, 200), width=2)
        d.line((67, 35, 1851, 35), fill=(202, 230, 238, 85), width=1)
        d.rounded_rectangle((55, 47, 62, 111), 3, fill=(224, 194, 131))
        d.rounded_rectangle((60, 126, 1860, 141), 8, fill=(43, 69, 105))
        # 显示器立在桌上，代码动画单独画在屏幕内。
        d.rounded_rectangle((52, 548, 740, 858), 12, fill=(9, 16, 26), outline=(74, 94, 118), width=3)
        d.line((68, 552, 722, 552), fill=(142, 166, 186), width=1)
        d.rectangle((76, 568, 716, 830), fill=(31, 30, 40))
        d.rectangle((76, 568, 716, 598), fill=(41, 41, 53))
        for x, color in ((94, (251, 102, 100)), (114, (245, 195, 74)), (134, (83, 205, 118))):
            d.ellipse((x, 577, x + 9, 586), fill=color)
        d.text((385, 571), 'WageSlave Code', font=self.font(14, True), fill=(198, 203, 222), anchor='ma')
        d.rectangle((368, 858, 416, 894), fill=(17, 27, 42))
        d.rounded_rectangle((282, 887, 508, 899), 6, fill=(16, 28, 43))
        d.line((293, 889, 496, 889), fill=(82, 105, 128), width=2)
        d.ellipse((685, 842, 690, 847), fill=(80, 220, 232))
        return image

    def _make_desk(self):
        yy, xx = np.mgrid[:180, :W]
        grain = np.sin(xx * .014 + np.sin(yy * .055) * 5) * 1.4 + np.sin(yy * 1.7 + xx * .0008) * 1.2
        shade = (yy / 180 * 12 + grain)[..., None]
        wood = np.clip(np.array([181, 137, 89]) - shade, 0, 255).astype(np.uint8)
        layer = Image.fromarray(wood).convert('RGBA')
        d = ImageDraw.Draw(layer)
        d.rectangle((0, 0, W, 12), fill=(240, 229, 205))
        d.line((0, 1, W, 1), fill=(255, 249, 227), width=2)
        d.rectangle((0, 12, W, 22), fill=(202, 172, 123))
        d.line((0, 22, W, 22), fill=(101, 75, 54), width=3)
        d.line((0, 26, W, 26), fill=(222, 184, 129), width=2)
        return layer

    @staticmethod
    def _make_chair():
        layer = Image.new('RGBA', (360, 430))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((8, 2, 352, 429), 76, fill=(9, 24, 42), outline=(102, 133, 157), width=3)
        d.rounded_rectangle((21, 15, 339, 414), 65, fill=(25, 49, 71), outline=(60, 87, 109), width=2)
        d.rounded_rectangle((65, 24, 295, 107), 30, fill=(19, 40, 61), outline=(93, 116, 131))
        for x in (50, 310):
            d.line((x, 130, x, 400), fill=(75, 99, 116), width=2)
        return layer

    def _make_props(self):
        layer = Image.new('RGBA', (W, 130))
        d = ImageDraw.Draw(layer)
        # 手账与马克杯都在桌前景，人物与鲨总仍由桌沿遮挡。
        d.ellipse((1430, 57, 1554, 79), fill=(30, 29, 25, 65))
        d.rounded_rectangle((1446, 18, 1528, 67), 13, fill=(236, 242, 238), outline=(122, 157, 172), width=2)
        d.ellipse((1446, 12, 1528, 35), fill=(168, 197, 208), outline=(245, 250, 242), width=4)
        d.ellipse((1454, 18, 1520, 31), fill=(84, 54, 38))
        d.arc((1510, 26, 1553, 63), -100, 100, fill=(230, 239, 235), width=9)
        d.ellipse((1471, 40, 1505, 57), fill=(65, 142, 181))
        d.polygon(((1501, 43), (1512, 36), (1510, 52)), fill=(65, 142, 181))
        d.rounded_rectangle((1583, 53, 1808, 102), 7, fill=(18, 31, 40, 95))
        d.polygon(((1590, 35), (1796, 42), (1805, 93), (1586, 84)), fill=(27, 57, 78), outline=(215, 185, 123))
        d.polygon(((1604, 39), (1784, 46), (1788, 80), (1601, 74)), fill=(240, 225, 194))
        for y in (49, 58, 67):
            d.line((1621, y, 1762, y + 5), fill=(183, 167, 139), width=1)
        d.line((1701, 31, 1785, 67), fill=(14, 37, 54), width=6)
        d.line((1701, 31, 1719, 38), fill=(222, 194, 129), width=3)
        return layer

    @staticmethod
    def _make_keyboard():
        layer = Image.new('RGBA', (380, 62))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((5, 43, 375, 61), 8, fill=(6, 16, 29, 130))
        d.polygon(((14, 2), (366, 2), (378, 50), (2, 50)),
                  fill=(23, 37, 58), outline=(85, 134, 164))
        d.polygon(((2, 50), (378, 50), (373, 58), (7, 58)), fill=(12, 25, 43))
        d.line((7, 58, 373, 58), fill=(58, 106, 139), width=2)
        for row in range(2):
            for col in range(13):
                x, y = 17 + col * 27, 8 + row * 14
                d.rounded_rectangle((x, y, x + 22, y + 10), 2, fill=(65, 87, 115), outline=(93, 119, 148))
        for x, width in ((17, 43), (65, 43), (113, 154), (272, 43), (320, 43)):
            d.rounded_rectangle((x, 37, x + width, 47), 2, fill=(65, 87, 115), outline=(93, 119, 148))
        return layer

    def _keyboard_layer(self, state):
        if not state.get('typing_enabled', True):
            return None
        from wage import avatar_layout, DEFAULTS
        center = float(state.get('avatar_center_x', avatar_layout(DEFAULTS, state)[3]))
        layer = self._keyboard.copy()
        speaking = bool(state.get('speech')) and state.get('speech_until') is None
        work_time = float(state.get('focus_s') or 0)
        if (state.get('status', 'working') == 'working' and not state.get('paused') and not speaking
                and work_time % 4.8 < 4.35):
            rate = 2.3 * math.sqrt(min(4, max(1, float(state.get('speed') or 1))))
            step = int(work_time * rate * 2)
            col = (4 if step % 2 == 0 else 8) + (step // 2) % 2
            x = 17 + col * 27
            ImageDraw.Draw(layer).rounded_rectangle((x, 22, x + 22, 32), 2,
                fill=(78, 208, 239), outline=(194, 249, 255))
        # 键盘完整放在桌面上，不能横跨桌沿、穿进人物腰部。
        return layer, round(center * W - layer.width / 2), 904

    def _wrap(self, text, font, width, max_lines=2):
        lines, line = [], ''
        chars = str(text).replace('\r', '')
        for index, char in enumerate(chars):
            if char == '\n' or (line and self._measure.textlength(line + char, font=font) > width):
                lines.append(line)
                line = ''
                if len(lines) == max_lines:
                    while self._measure.textlength(lines[-1] + '…', font=font) > width:
                        lines[-1] = lines[-1][:-1]
                    lines[-1] += '…'
                    return lines
                if char == '\n':
                    continue
            line += char
        if line:
            lines.append(line)
        return lines

    def render(self, state, now=None, foreground=True):
        now = time.monotonic() if now is None else now
        with self._lock:
            image = self._base.copy()
            center = float(state.get('avatar_center_x', .52))
            image.alpha_composite(self._chair, (round(center * W - 180), 560))
            d = ImageDraw.Draw(image)
            target = max(1, float(state.get('target') or 6000))
            wage = float(state.get('wage') or 0)
            status = str(state.get('status') or 'working')
            money = f'{abs(int(wage)):,}'
            prefix = '倒欠公司' if wage < 0 else '今日工资'
            d.text((76, 55), prefix, font=self.font(36, True), fill=_WHITE)
            d.text((266, 43), money, font=self.font(54, True), fill=_RED if wage < 0 else _YELLOW)
            if wage >= 0:
                width = int(d.textlength(money, font=self.font(54, True)))
                d.text((286 + width, 64), f'/ {int(target):,}', font=self.font(32), fill=_WHITE)
            progress = max(0, min(1, wage / target))
            d.text((1844, 66), f'{int(progress * 100)}%', font=self.font(29), fill=_BLUE, anchor='ra')
            if progress:
                d.rounded_rectangle((60, 126, 60 + max(15, int(1800 * progress)), 141), 8, fill=(83, 224, 255))
            stats = (f'专注 {int(state.get("focus_minutes") or 0)} 分钟 · '
                     f'白干 {int(state.get("white") or 0)} 次 · 被抓 {int(state.get("caught") or 0)} 次')
            d.text((50, 172), stats, font=self.font(29), fill=(215, 232, 251))
            d.text((1865, 172), str(state.get('clock') or ''), font=self.font(29), fill=(215, 232, 251), anchor='ra')
            label = {'working': '专心工作中', 'slacking': '偷偷摸鱼中', 'caught': '挨训中', 'offwork': '下班啦！'}.get(status, '上班中')
            if status == 'slacking' and state.get('slack_event'):
                label = state['slack_event']['label']
            color = {'working': (21, 188, 105), 'slacking': (224, 165, 38), 'caught': (210, 33, 80), 'offwork': (214, 80, 151)}.get(status, _GREEN)
            if state.get('paused'):
                label, color = '已暂停', (92, 116, 146)
            if state.get('away'):
                label, color = '上厕所中', (76, 132, 190)
            self._pill(d, (45, 227), label, color, _WHITE, 33)
            self._pill(d, (357, 234), f'手速 ×{float(state.get("speed") or 1):.1f}', (242, 246, 250), (197, 41, 99), 27)
            self._pill(d, (48, 310), '员工守则', _YELLOW, (66, 49, 8), 20)
            d.text((210, 316), '摸鱼就会白干', font=self.font(25, True), fill=_WHITE,
                   stroke_width=3, stroke_fill=(12, 38, 67))
            self._draw_ring(d, state)
            self._draw_code(d, state)
            if self.room_id:
                d.text((1848, 854), 'bilibili ' + self.room_id, font=self.font(16), fill=(160, 195, 225), anchor='ra')
            # 独立预览包含完整前景；合成链在 Live2D 后才叠前景，避免遮挡和重复字幕。
            if foreground:
                for layer, x, y in self.overlay_layers(state, now):
                    image.alpha_composite(layer, (x, y))
            else:
                image.alpha_composite(self._desk, (0, 900))
            image.putalpha(255)
            return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGBA2BGRA)

    def _pill(self, d, xy, text, bg, fg, size):
        x, y = xy
        font = self.font(size, True)
        width = int(d.textlength(text, font=font)) + 40
        d.rounded_rectangle((x, y, x + width, y + size + 28), (size + 28) // 2, fill=bg)
        d.text((x + 20, y + 7), text, font=font, fill=fg)

    def _draw_ring(self, d, state):
        status = state.get('status') or 'working'
        if status == 'offwork':
            return
        if status == 'slacking':
            title = (state.get('slack_event') or {}).get('title', '摸鱼')
            left, total, label = state.get('slack_left', 0), state.get('slack_total', 10), title + ' · 本轮白干'
            color = _YELLOW
        elif status == 'caught':
            left, total, label = state.get('caught_left', 0), state.get('caught_total', 5), '鲨总查岗'
            color = _RED
        else:
            left, total, label = state.get('work_left', 11), state.get('work_total', 25), '本轮结算倒计时'
            color = _GREEN if float(left) > 8 else _YELLOW
        cx, cy, radius = 168, 443, 78
        d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(13, 41, 82), outline=(31, 63, 108), width=9)
        ratio = max(0, min(1, float(left) / max(0.1, float(total))))
        if ratio:
            d.arc((cx - radius + 8, cy - radius + 8, cx + radius - 8, cy + radius - 8), -90, -90 + ratio * 360, fill=color, width=10)
        d.text((cx - 6, cy - 9), str(max(0, math.ceil(float(left)))), font=self.font(50, True), fill=_WHITE, anchor='mm')
        d.text((cx + 39, cy + 9), '秒', font=self.font(18), fill=_WHITE)
        d.text((cx, 529), label, font=self.font(14), fill=(170, 204, 232), anchor='mm')

    def _draw_away_screen(self, d, state):
        """上厕所中：显示器切成离开屏，和「人去工位空着」的状态对上。"""
        d.rectangle((76, 598, 716, 830), fill=(13, 31, 54))
        d.rounded_rectangle((94, 614, 698, 814), 14, fill=(18, 42, 70), outline=(64, 100, 138), width=3)
        d.text((396, 638), '上厕所中', font=self.font(56, True), fill=_WHITE, anchor='ma')
        d.text((396, 712), '马上回来，别走开~', font=self.font(25), fill=(170, 204, 232), anchor='ma')
        elapsed = float(state.get('away_elapsed') or 0)
        x = 366 + 20 * math.sin(elapsed * 1.3)
        self._mini_fish(d, x, 772, 27, (120, 213, 245))
        for i in range(3):
            d.text((x + 62 + i * 24, 762 - 5 * math.sin(elapsed * 2 + i * .8)),
                   '·', font=self.font(28, True), fill=(150, 190, 226))

    def _draw_code(self, d, state):
        status = state.get('status', 'working')
        if state.get('away'):
            self._draw_away_screen(d, state)
            return
        if status == 'slacking':
            self._draw_slack_screen(d, state)
            return
        if status == 'caught':
            d.rectangle((76, 598, 716, 830), fill=(255, 241, 250))
            d.text((395, 664), '·  ·', font=self.font(33, True), fill=(71, 46, 92), anchor='ma')
            d.text((395, 713), '摸鱼被抓到了！', font=self.font(24, True), fill=_RED, anchor='ma')
            event = state.get('slack_event')
            if event:
                d.text((395, 764), '刚才在：' + event['title'], font=self.font(19), fill=(118, 82, 120), anchor='ma')
            return
        # 只使用节目时间，暂停时不会继续写代码；手速随结算进度自然变化。
        phase = float(state.get('focus_s') or 0) * 2.4 + float(state.get('work_progress') or 0) * 24
        active = int(phase // 10) % len(_CODE_LINES)
        for i, text in enumerate(_CODE_LINES):
            y = 608 + i * 17
            d.text((91, y), str(i + 1).rjust(2), font=self.font(11), fill=(111, 112, 131))
            color = (146, 148, 166) if text.lstrip().startswith('#') else (217, 218, 228)
            if text.startswith(('from', 'class', 'def', '    return')):
                color = (220, 135, 192)
            if text.startswith('@'):
                color = (234, 202, 128)
            visible = text
            if (state.get('office') or {}).get('keyboard_left', 0) > 0 and i == active:
                visible, color = 'SyntaxError: 键盘开始摆烂了', _RED
            elif i == active and state.get('status', 'working') == 'working':
                visible = text[:int(phase % 10 / 10 * (len(text) + 1))]
            d.text((124, y), visible, font=self.font(12), fill=color)
            if i == active and int(phase * 2) % 2 == 0:
                x = 125 + int(d.textlength(visible, font=self.font(12)))
                d.rectangle((x, y + 2, x + 5, y + 13), fill=(220, 226, 244))
        if status == 'working':
            _active, result, _book, _office, blocked = self._play_parts(state)
            if result and not blocked:
                # 挑战结算后回到工作状态：结果继续占用显示器播报，不遮人物；查岗/投票让位。
                self._draw_play_result(d, result)

    @staticmethod
    def _mini_fish(d, x, y, size, color):
        d.ellipse((x-size, y-size*.55, x+size, y+size*.55), fill=color)
        d.polygon(((x+size*.7, y), (x+size*1.5, y-size*.65), (x+size*1.5, y+size*.65)), fill=color)
        d.ellipse((x-size*.6, y-size*.18, x-size*.4, y+size*.02), fill=(18, 43, 67))

    _PHASE_LABELS = {'waiting': '等待时机', 'bite': '咬钩了！', 'watch': '记住配方',
                     'input': '弹幕行动', 'ready': '完成待收工', 'inspection': '暂停中'}

    def _play_parts(self, state):
        play = state.get('slack_play') or {}
        active, result, book = play.get('active'), play.get('result'), play.get('book')
        office = state.get('office') or {}
        blocked = bool(office.get('round') or state.get('poll') or (office.get('result') and not active))
        return active, result, book, office, blocked

    def _play_votes(self, play, expected=0):
        choices = play.get('choices') or [f'抽屉{i + 1}' for i in range(expected)]
        votes = play.get('counts') or [0] * len(choices)
        return choices, votes

    def _play_panel_metrics(self, play):
        choices = play.get('choices') or []
        hint_lines = self._wrap(play.get('hint') or '', self.font(18), 596, 2)
        row_h = 24
        height = 12 + row_h * (2 + len(hint_lines) + bool(choices))
        return choices, hint_lines, row_h, height

    def _draw_play_panel(self, d, play, accent, elapsed):
        choices, hint_lines, row_h, height = self._play_panel_metrics(play)
        votes = play.get('counts') or []
        goal, progress = play['goal'], play['progress']
        left, phase = play['left'], play['phase']
        top = 830 - height
        d.rectangle((76, top, 716, 830), fill=(11, 22, 40))
        d.line((76, top, 716, top), fill=accent, width=2)
        y = top + 4
        blink = int(elapsed * 2.4) % 3 != 2
        tone = (_RED if phase == 'bite' and blink else _YELLOW if phase == 'ready' else
                (152, 168, 190) if phase == 'inspection' else accent)
        d.text((92, y), self._PHASE_LABELS.get(phase, '挑战中'), font=self.font(18, True), fill=tone)
        if goal > 1:
            pip_y = y + 10
            for index in range(goal):
                d.ellipse((240 + index * 17, pip_y - 5, 250 + index * 17, pip_y + 5),
                          fill=accent if index < progress else (70, 86, 108))
            d.text((252 + goal * 17, y), f'{progress}/{goal}', font=self.font(17, True), fill=_WHITE)
        d.text((702, y), f'剩 {math.ceil(left)} 秒', font=self.font(18, True),
               fill=_RED if left <= 3 else _WHITE, anchor='ra')
        y += row_h
        for line in hint_lines:
            d.text((92, y), line, font=self.font(18), fill=(225, 236, 248))
            y += row_h
        feedback = play.get('feedback') or ''
        if feedback:
            d.text((92, y), self._wrap('反馈 · ' + feedback, self.font(16), 596, 1)[0],
                   font=self.font(16), fill=(196, 214, 232))
        y += row_h
        for index, choice in enumerate(choices):
            width = 600 // len(choices)
            label = f'{choice} · {votes[index]}票' if votes else f'发「{choice}」'
            leading = bool(votes and votes[index] > 0 and votes[index] == max(votes))
            d.text((92 + index * width, y), self._wrap(label, self.font(17, True), width - 8, 1)[0],
                   font=self.font(17, True), fill=_YELLOW if leading else _BLUE)

    def _draw_play_result(self, d, result):
        """非活跃时利用显示器整屏播报结果/图鉴，不遮人物。"""
        success = result.get('success') is True
        tone = (96, 216, 150) if success else (255, 128, 140)
        d.rectangle((76, 598, 716, 830), fill=(15, 27, 48))
        d.rectangle((84, 606, 708, 822), outline=tone, width=3)
        d.ellipse((104, 622, 148, 666), outline=tone, width=4)
        if success:
            d.line((113, 645, 123, 656), fill=tone, width=5)
            d.line((123, 656, 141, 631), fill=tone, width=5)
        else:
            d.line((115, 633, 137, 655), fill=tone, width=5)
            d.line((137, 633, 115, 655), fill=tone, width=5)
        lines = self._wrap(str(result.get('title') or '挑战结果'), self.font(24, True), 500, 2)
        for index, line in enumerate(lines):
            d.text((164, 628 + index * 32), line, font=self.font(24, True), fill=tone)
        body_top = 628 + len(lines) * 32 + 12
        for index, line in enumerate(self._wrap(str(result.get('text') or ''), self.font(18), 576, 4)):
            d.text((104, body_top + index * 30), line, font=self.font(18), fill=_WHITE)
        try:
            left = math.ceil(max(0.0, float(result.get('left') or 0)))
        except (TypeError, ValueError):
            left = 0
        d.text((700, 800), f'剩 {left} 秒', font=self.font(13), fill=(150, 172, 198), anchor='ra')

    def _draw_fishing(self, d, band, elapsed, accent, play):
        """像素钓鱼主视觉；phase 来自后台快照，咬钩时给醒目水花与提竿提示。"""
        play = play or {}
        x0, y0, x1, y1 = band
        phase = str(play.get('phase') or 'waiting')
        dim = phase == 'inspection'
        sky = (122, 158, 168) if dim else (130, 197, 207)
        water = (44, 78, 98) if dim else (36, 111, 148)
        rope = (172, 180, 186) if dim else (243, 234, 209)
        horizon = y0 + max(16, round((y1 - y0) * .34))
        d.rectangle((x0, y0, x1, horizon), fill=sky)
        d.rectangle((x0, horizon, x1, y1), fill=water)
        for index in range(4):
            rx = x0 + 40 + int((index * 151 + elapsed * 26) % max(1, x1 - x0 - 110))
            ry = horizon + 5 + (index % 3) * 9
            d.line((rx, ry, rx + 24, ry), fill=(96, 168, 196), width=2)
        bob_x = 392
        bob_y = horizon + 8 + (6 if phase == 'bite' else 0)
        if phase == 'waiting' and not dim:
            bob_y += 3 * math.sin(elapsed * 2.4)
        d.line((x1 - 66, y0 + 2, bob_x, bob_y - 12), fill=rope, width=2)
        if phase == 'ready':
            # 提竿成功：鱼被吊出水面、滴水甩动。
            fx = bob_x - 30
            fy = y0 + 14 + 5 * math.sin(elapsed * 3.2)
            d.line((bob_x, bob_y - 12, fx + 12, fy + 6), fill=rope, width=2)
            self._mini_fish(d, fx, fy, 15, accent if not dim else (150, 180, 196))
            for index in range(4):
                d.ellipse((fx + 22 + index * 5, fy + 12 + index * 7, fx + 25 + index * 5, fy + 15 + index * 7),
                          fill=(198, 232, 246))
        d.ellipse((bob_x - 8, bob_y - 8, bob_x + 8, bob_y + 8), fill=(250, 247, 240))
        d.pieslice((bob_x - 8, bob_y - 8, bob_x + 8, bob_y + 8), 180, 360, fill=(235, 92, 106))
        if phase == 'bite':
            # 咬钩：扩散水花环 + 飞散水珠 + 闪烁“提竿！”牌，一眼可辨。
            for ring in range(3):
                r = 12 + ring * 10 + int(elapsed * 26 % 10)
                d.arc((bob_x - r, bob_y - max(4, r // 2), bob_x + r, bob_y + max(4, r // 2)),
                      0, 360, fill=(236, 250, 255), width=3)
            for index in range(6):
                angle = index * 1.05 + .3
                hx = bob_x + math.cos(angle) * (16 + elapsed * 34 % 14)
                hy = bob_y - 6 - math.sin(angle) * (13 + elapsed * 40 % 17)
                d.ellipse((hx - 2, hy - 2, hx + 2, hy + 2), fill=(240, 252, 255))
            if int(elapsed * 3) % 2 == 0:
                d.rounded_rectangle((bob_x + 26, y0 + 2, bob_x + 150, y0 + 34), 8,
                                    fill=(255, 250, 240), outline=(218, 60, 92), width=3)
                d.text((bob_x + 88, y0 + 18), '提竿！', font=self.font(20, True),
                       fill=(218, 60, 92), anchor='mm')
                d.text((bob_x - 26, bob_y - 26), '!', font=self.font(26, True),
                       fill=_YELLOW, anchor='mm')

    def _draw_cat(self, d, band, elapsed, accent, play):
        """工位撸猫：团睡橘猫、摆尾、循环抚摸手与呼噜波纹。"""
        play = play or {}
        x0, y0, x1, y1 = band
        phase = str(play.get('phase') or 'input')
        try:
            progress = int(play.get('progress') or 0)
        except (TypeError, ValueError):
            progress = 0
        desk = y1 - max(7, round((y1 - y0) * .16))
        d.rectangle((x0, desk, x1, y1), fill=(198, 152, 104))
        d.line((x0, desk, x1, desk), fill=(150, 108, 70), width=2)
        d.rounded_rectangle((x1 - 236, desk - 11, x1 - 64, desk + 1), 4, fill=(63, 84, 108))
        for index in range(6):
            d.rectangle((x1 - 228 + index * 26, desk - 8, x1 - 214 + index * 26, desk - 3), fill=(128, 156, 181))
        cx = x0 + 250
        sway = 2 * math.sin(elapsed * 1.6)
        d.line((cx + 66, desk - 14, cx + 108 + 12 * math.sin(elapsed * 2.6), desk - 34),
               fill=(243, 209, 166), width=12)
        d.ellipse((cx - 92, desk - 40 + sway, cx + 78, desk + 4), fill=(243, 209, 166),
                  outline=(181, 134, 92), width=2)
        for index in range(3):
            d.arc((cx - 52 + index * 34, desk - 42 + sway, cx - 26 + index * 34, desk - 22 + sway),
                  210, 330, fill=(198, 150, 100), width=3)
        hx, hy = cx - 104, desk - 32 + sway
        d.ellipse((hx - 32, hy - 24, hx + 32, hy + 24), fill=(246, 216, 176),
                  outline=(181, 134, 92), width=2)
        d.polygon(((hx - 25, hy - 14), (hx - 14, hy - 33), (hx - 3, hy - 16)), fill=(246, 216, 176))
        d.polygon(((hx + 3, hy - 16), (hx + 15, hy - 33), (hx + 26, hy - 13)), fill=(246, 216, 176))
        eye_y = hy + 1
        alert = phase == 'inspection'
        done = phase == 'ready' or progress >= 3
        if done:
            d.arc((hx - 24, eye_y - 4, hx - 8, eye_y + 6), 20, 160, fill=(94, 66, 46), width=2)
            d.arc((hx + 8, eye_y - 4, hx + 24, eye_y + 6), 20, 160, fill=(94, 66, 46), width=2)
        elif alert:
            d.ellipse((hx - 22, eye_y - 5, hx - 10, eye_y + 5), fill=(84, 112, 66))
            d.ellipse((hx + 10, eye_y - 5, hx + 22, eye_y + 5), fill=(84, 112, 66))
        else:
            d.ellipse((hx - 20, eye_y - 4, hx - 12, eye_y + 4), fill=(72, 52, 40))
            d.ellipse((hx + 12, eye_y - 4, hx + 20, eye_y + 4), fill=(72, 52, 40))
        d.polygon(((hx - 4, eye_y + 7), (hx + 4, eye_y + 7), (hx, eye_y + 12)), fill=(222, 138, 138))
        for side in (-1, 1):
            d.line((hx + side * 8, eye_y + 10, hx + side * 30, eye_y + 6), fill=(204, 170, 132), width=1)
            d.line((hx + side * 8, eye_y + 12, hx + side * 30, eye_y + 15), fill=(204, 170, 132), width=1)
        if done:
            for index in range(2):
                d.text((hx + 26 + index * 15, hy - 26 - index * 13 - 3 * math.sin(elapsed * 2)),
                       'z', font=self.font(13 + index * 4, True), fill=(150, 140, 190))
        if alert and int(elapsed * 4) % 2 == 0:
            d.text((x1 - 118, y0 + 2), '有人！', font=self.font(16, True), fill=(206, 84, 84))
        if not done and not alert:
            t = (elapsed % 2.2) / 2.2
            px = int(cx - 36 + 108 * t)
            py = desk - 52 + int(9 * math.sin(t * math.pi))
            d.rounded_rectangle((px - 13, py - 20, px + 13, py), 7, fill=(248, 221, 191),
                                outline=(205, 159, 119), width=2)
            if t > .45:
                r = int(9 + (t - .45) * 60)
                d.arc((hx - r, hy - 26 - r, hx + r, hy - 26 + r), 205, 335, fill=accent, width=2)
        else:
            r = int(8 + (elapsed % 1.1) * 15)
            d.arc((hx - r, hy - 30 - r, hx + r, hy - 30 + r), 205, 335, fill=accent, width=2)

    def _draw_coffee(self, d, band, elapsed, ink, play):
        """记忆咖啡：俯视杯口、晃动摇液、蒸汽与“背配方”步骤气泡（watch 显示、input 变问号）。"""
        play = play or {}
        x0, y0, x1, y1 = band
        phase = str(play.get('phase') or 'watch')
        try:
            progress = int(play.get('progress') or 0)
        except (TypeError, ValueError):
            progress = 0
        cx = 420
        ry = max(10, round((y1 - y0) * .3))
        ctop, cbot = y0 + ry + 3, y1 - 4
        if phase == 'inspection':
            outer, liquid, steam = (208, 200, 188), (128, 108, 94), (176, 176, 176)
        else:
            outer, liquid, steam = (252, 246, 235), (160, 112, 76), (255, 252, 245)
        d.rectangle((cx - 58, ctop, cx + 58, cbot), fill=outer, outline=(150, 118, 88), width=2)
        d.polygon(((cx + 56, ctop + 8), (cx + 82, ctop + 14), (cx + 74, cbot - 10),
                   (cx + 56, cbot - 14), (cx + 66, ctop + 18), (cx + 56, ctop + 18)), fill=outer,
                  outline=(150, 118, 88))
        d.ellipse((cx - 58, ctop - ry, cx + 58, ctop + ry), fill=outer, outline=(150, 118, 88), width=2)
        d.ellipse((cx - 52, ctop - ry + 5, cx + 52, ctop + ry - 5), fill=liquid)
        if phase != 'inspection':
            d.ellipse((cx - 34, ctop - 5 + 3 * math.sin(elapsed * 2.6), cx + 2, ctop + 7),
                      fill=(196, 158, 118))
        for index in range(3):
            bx = cx - 30 + index * 30
            for step in range(4):
                py = ctop - ry - 7 - step * 7 - (elapsed * 9 % 7)
                if py < y0 + 2:
                    continue
                px = bx + 4 * math.sin(elapsed * 2 + step * .9 + index)
                d.ellipse((px - 2, py - 2, px + 2, py + 2), fill=steam)
        slot_y = min(y0 + 16, cbot - 18)
        if phase in ('watch', 'input'):
            accent = (214, 158, 96)
            for index in range(3):
                sx = x0 + 52 + index * 58
                known = phase == 'watch' or index < progress
                d.ellipse((sx - 13, slot_y - 13, sx + 13, slot_y + 13),
                          fill=(255, 251, 240) if known else (226, 213, 190),
                          outline=accent if known else (168, 146, 116), width=2)
                mark = str(index + 1) if phase == 'watch' else ('√' if index < progress else '?')
                d.text((sx, slot_y), mark, font=self.font(14, True), fill=ink, anchor='mm')

    def _draw_treasure(self, d, band, elapsed, accent, play):
        """抽屉寻宝：三格抽屉、扫光与闪烁星点；唯一最高票抽屉描金提示。"""
        play = play or {}
        x0, y0, x1, y1 = band
        phase = str(play.get('phase') or 'input')
        _choices, votes = self._play_votes(play, expected=3)
        lead = (votes.index(max(votes)) if max(votes) > 0 and votes.count(max(votes)) == 1 else -1)
        cx = (x0 + x1) // 2
        top, bottom = y0 + 4, y1 - 4
        gap = 5
        each = max(12, (bottom - top - gap * 4) // 3)
        muted = phase == 'inspection'
        for index in range(3):
            dy0 = top + gap + index * (each + gap)
            dy1 = min(bottom - 1, dy0 + each)
            d.rounded_rectangle((cx - 150, dy0, cx + 150, dy1), 6,
                                fill=(84, 74, 66) if muted else (104, 72, 48),
                                outline=(70, 46, 28), width=2)
            d.rounded_rectangle((cx - 22, (dy0 + dy1) // 2 - 3, cx + 22, (dy0 + dy1) // 2 + 3),
                                3, fill=(214, 176, 108))
            d.text((cx - 134, (dy0 + dy1) // 2), str(index + 1), font=self.font(13, True),
                   fill=(238, 222, 196), anchor='lm')
            if index == lead and not muted:
                d.rounded_rectangle((cx - 154, dy0 - 2, cx + 154, dy1 + 2), 8, outline=accent, width=3)
                glow = .5 + .5 * math.sin(elapsed * 4)
                tone = tuple(int(150 + (color - 150) * glow) for color in accent)
                d.ellipse((cx + 118, (dy0 + dy1) // 2 - 4, cx + 126, (dy0 + dy1) // 2 + 4), fill=tone)
        glint_x = int(x0 + 30 + (elapsed * 120) % max(1, x1 - x0 - 60))
        d.polygon(((glint_x, top), (glint_x + 16, top), (glint_x + 2, bottom), (glint_x - 14, bottom)),
                  fill=(255, 255, 240))
        for index in range(6):
            if int(elapsed * 2 + index * .7) % 3 != 0:
                continue
            sx = x0 + 40 + (index * 107) % max(1, x1 - x0 - 80)
            sy = y0 + 10 + (index * 53) % max(1, y1 - y0 - 20)
            d.line((sx - 6, sy, sx + 6, sy), fill=accent, width=2)
            d.line((sx, sy - 6, sx, sy + 6), fill=accent, width=2)

    def _draw_slack_screen(self, d, state):
        """程序绘制小剧场与弹幕挑战指令区；只使用节目时间，暂停后画面不继续播放。"""
        kind = (state.get('slack_event') or {}).get('id', 'anime')
        event = SLACK_BY_ID.get(kind, SLACK_BY_ID['anime'])
        kind, accent = event['id'], event['accent']
        elapsed = float(state.get('slack_elapsed') or 0)
        light = kind in ('shopping', 'snack', 'daydream', 'cat', 'coffee')
        bg, ink = ((242, 237, 224), (41, 62, 82)) if light else ((17, 31, 55), _WHITE)
        d.rectangle((76, 598, 716, 830), fill=bg)
        d.rectangle((166, 568, 716, 598), fill=(33, 47, 68))
        d.text((398, 571), event['title'], font=self.font(14, True), fill=accent, anchor='ma')
        active, result, _book, _office, blocked = self._play_parts(state)
        play = active if active and active.get('kind') == kind else None
        band_bottom = 804
        if play:
            # 上部视觉、下部指令：视觉上界由面板高度反推，始终留在显示器字幕区内。
            band_bottom = max(642, 830 - self._play_panel_metrics(play)[3])
        if kind == 'game' and play:
            self._draw_fishing(d, (76, 598, 716, band_bottom), elapsed, accent, play)
        elif kind == 'cat':
            self._draw_cat(d, (76, 598, 716, band_bottom if play else 804), elapsed, accent, play)
        elif kind == 'coffee':
            self._draw_coffee(d, (76, 598, 716, band_bottom if play else 804), elapsed, ink, play)
        elif kind == 'treasure':
            self._draw_treasure(d, (76, 598, 716, band_bottom if play else 804), elapsed, accent, play)
        elif kind == 'anime':
            d.rounded_rectangle((100, 612, 690, 792), 12, fill=(64, 60, 113))
            d.ellipse((557, 627, 613, 683), fill=(255, 232, 171))
            d.polygon(((100, 763), (214, 663), (339, 774)), fill=(103, 119, 172))
            d.polygon(((390, 792), (542, 683), (690, 792)), fill=(91, 102, 156))
            x = 361 + 22 * math.sin(elapsed * 1.8)
            self._mini_fish(d, x, 705 + 7 * math.sin(elapsed * 2.3), 47, (120, 213, 245))
            d.text((119, 620), '海底小剧场', font=self.font(20, True), fill=_WHITE)
            d.line((119, 779, 670, 779), fill=(144, 144, 192), width=3)
            d.line((119, 779, 119 + 551 * ((elapsed / 10) % 1), 779), fill=accent, width=4)
        elif kind == 'shorts':
            d.text((112, 642), '再刷最后一个…', font=self.font(25, True), fill=_WHITE)
            d.text((112, 691), f'已经是第 {int(elapsed * .9) + 1} 个了', font=self.font(18), fill=accent)
            palettes = ((78, 126, 172), (142, 94, 158), (53, 147, 153))
            d.rounded_rectangle((457, 607, 580, 798), 17, fill=(6, 13, 27), outline=(130, 159, 187), width=3)
            d.rounded_rectangle((465, 622, 572, 782), 10, fill=palettes[int(elapsed) % 3])
            self._mini_fish(d, 511, 695, 26, (245, 213, 166))
            d.rounded_rectangle((488, 611, 550, 619), 4, fill=(6, 13, 27))
            for y in (663, 704, 745):
                d.ellipse((601, y, 618, y+17), fill=accent)
        elif kind == 'shopping':
            for i, (name, price) in enumerate((('零食补给', '19'), ('机械键盘', '299'), ('鱼鱼抱枕', '59'))):
                x = 99 + i * 198
                d.rounded_rectangle((x, 613, x+184, 789), 9, fill=(255, 252, 244), outline=(219, 206, 184))
                if i == 0:
                    d.rounded_rectangle((x+62, 634, x+121, 704), 6, fill=(242, 184, 112))
                    d.ellipse((x+77, 656, x+106, 685), fill=(255, 232, 181))
                elif i == 1:
                    d.rounded_rectangle((x+24, 649, x+160, 696), 5, fill=(52, 76, 103))
                    for row in range(2):
                        for col in range(8):
                            d.rectangle((x+31+col*15, 658+row*14, x+41+col*15, 667+row*14), fill=(133, 177, 196))
                else:
                    self._mini_fish(d, x+84, 670, 40, (103, 181, 217))
                d.text((x+14, 714), name, font=self.font(18, True), fill=ink)
                d.text((x+14, 751), '¥' + price, font=self.font(21, True), fill=(204, 91, 91))
                if i == int(elapsed / 1.6) % 3:
                    d.text((x+164, 753), '+', font=self.font(22, True), fill=(56, 154, 134), anchor='ra')
        elif kind == 'snack':
            d.text((109, 632), '工位零食补给站', font=self.font(25, True), fill=ink)
            d.text((109, 680), '包装袋小声一点…', font=self.font(20), fill=(157, 106, 83))
            d.rounded_rectangle((506, 621, 628, 781), 9, fill=(241, 177, 111), outline=(197, 131, 77), width=3)
            d.line((511, 637, 623, 637), fill=(255, 222, 161), width=6)
            d.text((567, 651), '鱼片？', font=self.font(21, True), fill=(112, 69, 48), anchor='ma')
            for i in range(3):
                x, y = 450 + i * 44, 757 + 6 * math.sin(i + elapsed)
                d.ellipse((x-22, y-13, x+22, y+13), fill=(246, 212, 144), outline=(201, 153, 86), width=2)
        elif kind == 'meeting':
            d.text((103, 607), '会议议题：如何准时下班', font=self.font(18, True), fill=_WHITE)
            for i, name in enumerate(('我', '空气同事', '会议纪要', '静音中')):
                x, y = 102 + (i % 2) * 299, 641 + (i // 2) * 75
                d.rounded_rectangle((x, y, x+282, y+64), 8, fill=(36, 65, 85), outline=accent if i == int(elapsed) % 4 else (69, 91, 109), width=2)
                self._mini_fish(d, x+35, y+30, 18, accent)
                d.text((x+76, y+19), name, font=self.font(17), fill=_WHITE)
        elif kind == 'nap':
            d.ellipse((484, 622, 576, 714), fill=(248, 224, 155))
            d.ellipse((511, 610, 585, 688), fill=bg)
            for i in range(11):
                x, y = 116 + i*49, 627 + (i*43) % 139
                d.ellipse((x, y, x+3, y+3), fill=(175, 194, 226))
            d.text((158, 665), '闭眼编译中', font=self.font(27, True), fill=_WHITE)
            for i in range(3):
                d.text((350+i*37, 724-i*23 - 5*math.sin(elapsed*1.5)), 'z', font=self.font(24+i*5, True), fill=accent)
        elif kind == 'game':
            d.rectangle((96, 613, 696, 793), fill=(36, 111, 148))
            d.rectangle((96, 613, 696, 650), fill=(130, 197, 207))
            d.text((113, 619), '像素钓鱼 · 偷玩一局', font=self.font(20, True), fill=(20, 57, 74))
            d.line((372, 637, 394, 716), fill=(243, 234, 209), width=2)
            for i in range(3):
                x = 153 + ((i*157 + elapsed*38) % 478)
                self._mini_fish(d, x, 690+i*32, 16, ((245, 209, 135), (155, 223, 221), (244, 171, 155))[i])
        else:
            d.rectangle((96, 612, 696, 793), fill=(184, 222, 231))
            for i in range(3):
                x = 142 + ((i*160 + elapsed*9) % 440)
                d.ellipse((x-29, 634+i*23, x+48, 667+i*23), fill=(246, 249, 239))
            self._mini_fish(d, 371 + 20*math.sin(elapsed), 726, 31, (91, 163, 191))
            d.text((398, 621), '脑内正在加载…', font=self.font(23, True), fill=ink, anchor='ma')
        if play:
            self._draw_play_panel(d, play, accent, elapsed)
        else:
            d.text((398, 804), '鲨总来了记得提醒我', font=self.font(15), fill=(133, 158, 184) if not light else (116, 132, 139), anchor='ma')
            if result and not blocked:
                # 无查岗/任务投票/观众投票时，用显示器整屏播报挑战结果或图鉴，不占人物区域。
                self._draw_play_result(d, result)

    def _speech_layer(self, state, now):
        text = str(state.get('speech') or '').strip()
        until = state.get('speech_until')
        if not text or (until is not None and now >= float(until)):
            return None
        if self._speech_cache is None or self._speech_cache[0] != text:
            font = self.font(38, True)
            lines = self._wrap(text, font, 1610, 2)
            layer = Image.new('RGBA', (1700, 104))
            d = ImageDraw.Draw(layer)
            for index, line in enumerate(lines):
                d.text((850, index * 49 + 4), line, font=font, fill=_BLUE,
                       stroke_width=4, stroke_fill=(15, 36, 64), anchor='ma')
            self._speech_cache = (text, layer)
        return self._speech_cache[1], 110, 976

    def _boss_layers(self, state, now):
        caught = state.get('status') == 'caught'
        office = state.get('office') or {}
        mode = office.get('boss_pose') or ('scold' if caught else 'idle' if state.get('poll') else '')
        if not mode:
            self._boss_mode = None
            return []
        if self._boss_mode != mode:
            self._boss_mode, self._boss_elapsed = mode, 0.0
        elif not state.get('paused'):
            self._boss_elapsed += max(0, now - self._boss_last_time)
        self._boss_last_time = now
        frames, ends, total = _boss_animation(mode)
        index = min(len(frames) - 1, bisect.bisect_right(ends, self._boss_elapsed % total)) if frames else -1
        if not frames:
            return []
        height = round(_BOSS_HEIGHT * max(.6, min(1.5, float(state.get('boss_scale') or 1))))
        layer = _boss_frame(mode, index, height)
        # 鲨总与主播都站在桌后，桌面负责遮住下半身。
        # 头部与主播接近大小，保留顶部工资栏和左侧互动卡片。
        x = W - 35 - layer.width
        badge = Image.new('RGBA', (280, 48))
        d = ImageDraw.Draw(badge)
        d.rounded_rectangle((0, 0, 279, 47), 14, fill=(9, 22, 39, 235), outline=(180, 190, 207, 180))
        caption = {'inspect': '鲨总快来了', 'assign': '鲨总派活', 'angry': '鲨总训话',
                   'approve': '鲨总批准', 'scold': '鲨总查岗', 'idle': '鲨总围观'}.get(mode, '鲨总')
        d.text((140, 24), caption, font=self.font(27, True), fill=_WHITE, anchor='mm')
        layers = [(layer, x, _BOSS_TOP), (badge, W - 315, _BOSS_TOP - 50)]
        if not caught:
            return layers
        stamp = Image.new('RGBA', (520, 146))
        sd = ImageDraw.Draw(stamp)
        sd.rounded_rectangle((5, 5, 515, 138), 8, fill=(255, 248, 249, 246), outline=(218, 34, 77), width=6)
        sd.text((260, 13), f'-{float(state.get("deduction", 500)):g}', font=self.font(80, True), fill=(215, 32, 69), anchor='ma')
        stamp = stamp.rotate(10, resample=Image.Resampling.BICUBIC, expand=True)
        return layers + [(stamp, 760, 670)]

    def overlay_layers(self, state, now):
        layers = self._boss_layers(state, now) + [(self._desk, 0, 900)]
        layers.append((self._props, *self._props_xy))
        keyboard = self._keyboard_layer(state)
        if keyboard:
            layer, x, y = keyboard
            layers.append((layer, x, y))
        layers.extend(self._office_layers(state))
        active, _result, _book, office, blocked = self._play_parts(state)
        if active and blocked:
            pause_text = active['hint']
            strip = Image.new('RGBA', (440, 42))
            sd = ImageDraw.Draw(strip)
            sd.rounded_rectangle((0, 0, 439, 41), 12, fill=(12, 28, 52, 238), outline=(240, 202, 120, 200), width=2)
            sd.text((14, 11), self._wrap(pause_text, self.font(16, True), 412, 1)[0],
                    font=self.font(16, True), fill=_YELLOW)
            layers.append((strip, 280, 354))
        presenter_status = state.get('presenter_status')
        if presenter_status:
            notice = Image.new('RGBA', (650, 52))
            d = ImageDraw.Draw(notice)
            d.rounded_rectangle((0, 0, 649, 51), 15, fill=(8, 31, 62, 235))
            d.text((20, 11), self._wrap(presenter_status, self.font(23), 610, 1)[0],
                   font=self.font(23), fill=_YELLOW)
            layers.append((notice, 765, 825))
        poll = state.get('poll')
        if poll:
            card = Image.new('RGBA', (632, 266))
            d = ImageDraw.Draw(card)
            d.rounded_rectangle((0, 0, 631, 265), 14, fill=(8, 34, 66, 248), outline=_YELLOW, width=3)
            d.text((22, 14), '紧急发起投票', font=self.font(22, True), fill=_YELLOW)
            d.text((607, 14), f'{math.ceil(poll["left"])} 秒', font=self.font(21), fill=_WHITE, anchor='ra')
            d.text((22, 57), '打工主播该不该加薪？', font=self.font(28, True), fill=_WHITE)
            yes, no = int(poll.get('yes') or 0), int(poll.get('no') or 0)
            d.text((26, 106), str(yes), font=self.font(46, True), fill=(102, 182, 255))
            d.text((603, 106), str(no), font=self.font(46, True), fill=_RED, anchor='ra')
            d.rounded_rectangle((27, 172, 604, 188), 8, fill=_RED)
            width = int(577 * ((yes / (yes + no)) if yes + no else 0.5))
            if width > 0:
                d.rounded_rectangle((27, 172, 27 + width, 188), 8, fill=(102, 182, 255))
            d.text((25, 207), poll.get('result') or '发 1：该加薪！      发 2：做梦，继续敲',
                   font=self.font(22, True), fill=_WHITE)
            layers.append((card, 80, 566))
        viewer = state.get('viewer')
        if viewer:
            text = str(viewer.get('name') or '观众') + '：' + str(viewer.get('text') or '')
            font = self.font(22, True)
            line = self._wrap(text, font, 710, 1)[0]
            width = int(self._measure.textlength(line, font=font)) + 40
            layer = Image.new('RGBA', (width, 44))
            d = ImageDraw.Draw(layer)
            d.rounded_rectangle((0, 0, width - 1, 43), 20, fill=(255, 255, 252, 240))
            d.text((20, 5), line, font=font, fill=(39, 48, 67))
            layers.append((layer, 45, 928))
        speech = self._speech_layer(state, now)
        if speech:
            layers.append(speech)
        for item in state.get('floaters') or []:
            age = now - float(item.get('at', now))
            if not 0 <= age < 1.8:
                continue
            alpha = int(255 * (1 - age / 1.8))
            text = str(item.get('text') or '')
            font = self.font(int(item.get('size') or 30), True)
            width = int(self._measure.textlength(text, font=font)) + 16
            layer = Image.new('RGBA', (width, font.size + 20))
            color = tuple(item.get('color') or _GREEN)
            ImageDraw.Draw(layer).text((8, 2), text, font=font, fill=(*color, alpha),
                                      stroke_width=2, stroke_fill=(10, 34, 58, alpha))
            layers.append((layer, int(item.get('x') or 530), int(item.get('y') or 160) - int(age * 55)))
        if state.get('status') == 'offwork':
            card = Image.new('RGBA', (760, 224))
            d = ImageDraw.Draw(card)
            d.rounded_rectangle((0, 0, 759, 223), 26, fill=(8, 32, 63, 245), outline=_YELLOW, width=3)
            d.text((380, 25), '今日工资到账 · 下班！', font=self.font(43, True), fill=_YELLOW, anchor='ma')
            d.text((380, 122), f'{math.ceil(float(state.get("next_day_left") or 0))} 秒后重新打卡', font=self.font(25), fill=_WHITE, anchor='ma')
            layers.append((card, 580, 510))
        return layers

    def _book_summary(self, book):
        """图鉴摘要：全部取自后台 book，缺字段安全回退。"""
        if not isinstance(book, dict):
            return ''
        unlocked = book.get('unlocked') if isinstance(book.get('unlocked'), list) else []
        fish = book.get('fish') if isinstance(book.get('fish'), list) else []
        try:
            total = int(book.get('total') or 0)
            fish_total = int(book.get('fish_total') or 0)
            wins = int(book.get('wins') or 0)
            streak = int(book.get('streak') or 0)
            best = int(book.get('best_streak') or 0)
        except (TypeError, ValueError):
            total, fish_total, wins, streak, best = 0, 0, 0, 0, 0
        parts = [f'图鉴{len(unlocked)}/{total}'] if total else []
        if fish_total:
            parts.append(f'鱼{len(fish)}/{fish_total}')
        parts.append(f'{wins}胜')
        parts.append(f'连胜{streak}/最高{best}')
        return ' · '.join(parts)

    def _office_layers(self, state):
        office = state.get('office')
        _active, _result, book, _office, _blocked = self._play_parts(state)
        if not office and not book:
            return []
        layers = []
        badge = Image.new('RGBA', (715, 78))
        d = ImageDraw.Draw(badge)
        d.rounded_rectangle((0, 0, 714, 77), 18, fill=(10, 30, 59, 225), outline=(112, 166, 209, 140))
        if office:
            d.text((18, 10), f"{office['rank']} · EXP {office['xp']} · 公共积分 {office['wallet']}",
                   font=self.font(23, True), fill=_YELLOW)
            story = f"{office['story']} · {self._book_summary(book)}" if book else str(office['story'])
            d.text((18, 45), self._wrap(story, self.font(17), 684, 1)[0], font=self.font(17), fill=_BLUE)
        else:
            unlocked = book.get('unlocked') if isinstance(book.get('unlocked'), list) else []
            d.text((18, 10), self._wrap(f"摸鱼图鉴 · {self._book_summary(book)}", self.font(21, True), 684, 1)[0],
                   font=self.font(21, True), fill=_YELLOW)
            d.text((18, 45), self._wrap('发「摸鱼图鉴」看已解锁场景与鱼获收藏', self.font(17), 684, 1)[0],
                   font=self.font(17), fill=_BLUE)
        layers.append((badge, 675, 227))
        if not office:
            return layers
        rule = office.get('rule')
        if rule:
            card = Image.new('RGBA', (970, 44))
            d = ImageDraw.Draw(card)
            d.rounded_rectangle((0, 0, 969, 43), 16, fill=(17, 40, 73, 250))
            status = '已违规' if rule.get('broken') else f"剩 {math.ceil(rule['left'])} 秒"
            d.text((15, 7), f"临时守则：{rule['title']} · {status}", font=self.font(22, True), fill=_YELLOW)
            layers.append((card, 207, 310))
        active, result = office.get('round'), office.get('result')
        if (state.get('slack_play') or {}).get('active') and not active:
            result = None
        if active or result:
            card = Image.new('RGBA', (676, 310))
            d = ImageDraw.Draw(card)
            d.rounded_rectangle((0, 0, 675, 309), 16, fill=(8, 31, 62, 250), outline=_YELLOW, width=3)
            if active:
                titles = {'rescue': '全场打掩护', 'task': '离谱需求任务', 'rules': '投票改员工守则'}
                d.text((20, 14), titles[active['kind']], font=self.font(23, True), fill=_YELLOW)
                d.text((652, 14), f"{math.ceil(active['left'])} 秒", font=self.font(22), fill=_WHITE, anchor='ra')
                for i, line in enumerate(self._wrap(active['title'], self.font(23, True), 625, 2)):
                    d.text((22, 59 + i * 29), line, font=self.font(23, True), fill=_WHITE)
                for index, choice in enumerate(active['choices']):
                    # 守则名称较长时也限制在卡片内，不遮住计票。
                    line = self._wrap(choice, self.font(21), 490, 1)[0]
                    d.text((24, 130 + index * 43), f'发 {index+1}：{line}', font=self.font(21), fill=_BLUE)
                    d.text((650, 130 + index * 43), f"{active['counts'][index]} 票", font=self.font(21, True), fill=_YELLOW, anchor='ra')
                d.text((22, 273), '每人一票，可改票；首次参与 +1 公共积分', font=self.font(16), fill=(163, 191, 215))
            else:
                d.text((23, 24), result['title'], font=self.font(29, True), fill=_YELLOW)
                for i, line in enumerate(self._wrap(result.get('text') or '', self.font(23), 620, 4)):
                    d.text((25, 85 + i * 38), line, font=self.font(23), fill=_WHITE)
            layers.append((card, 66, 558))
        items = '  '.join(f'{name}×{count}' for name, count in office['items'].items())
        strip = Image.new('RGBA', (720, 28))
        d = ImageDraw.Draw(strip)
        d.rounded_rectangle((0, 0, 719, 27), 8, fill=(9, 27, 49, 235))
        d.text((8, 2), '道具柜  ' + items, font=self.font(16), fill=_BLUE)
        layers.append((strip, 45, 868))
        buffs = []
        if office.get('coffee_left', 0) > 0:
            buffs.append(f"咖啡加速 {math.ceil(office['coffee_left'])}s")
        if office.get('shield'):
            buffs.append('挡箭牌已就绪')
        if office.get('pillow_left', 0) > 0:
            buffs.append('枕头：下次成功摸鱼+30')
        if office.get('keyboard_left', 0) > 0:
            buffs.append('坏键盘正在捣乱')
        if buffs:
            layer = Image.new('RGBA', (610, 95))
            d = ImageDraw.Draw(layer)
            for i, text in enumerate(buffs[:4]):
                d.text((0, i * 23), text, font=self.font(17, True), fill=_YELLOW)
            layers.append((layer, 300, 401))
        return layers

    def apply_overlays(self, canvas, state, now):
        with self._lock:
            for layer, x, y in self.overlay_layers(state, now):
                # 桌沿在人物前面遮住腰部，形成坐在工位后的半身构图。
                if layer is self._desk and canvas.shape[0] >= 1080 and canvas.shape[1] >= 1920:
                    canvas[900:1080, :1920, :3] = self._desk_bgra[..., :3]
                else:
                    self._blend(canvas, layer, x, y)

    @staticmethod
    def _blend(canvas, layer, x, y):
        src = np.asarray(layer)
        height, width = src.shape[:2]
        canvas_h, canvas_w = canvas.shape[:2]
        x0, y0, x1, y1 = max(0, x), max(0, y), min(canvas_w, x + width), min(canvas_h, y + height)
        if x1 <= x0 or y1 <= y0:
            return
        block = src[y0-y:y1-y, x0-x:x1-x]
        alpha = block[..., 3].astype(np.float32) / 255
        dst = canvas[y0:y1, x0:x1]
        # PIL 层是 RGBA，采集画布是 BGRA。
        bgra = cv2.cvtColor(block, cv2.COLOR_RGBA2BGRA)
        saved_alpha = dst[..., 3].copy()
        cv2.blendLinear(dst, bgra, 1 - alpha, alpha, dst=dst)
        dst[..., 3] = saved_alpha


class WageForeground:
    def __init__(self, app):
        self.app = app

    def apply(self, canvas):
        if not getattr(self.app, '_is_wage', lambda: False)():
            return False
        engine = getattr(self.app, 'wage', None)
        return bool(engine is not None and engine.foreground(canvas))


def demo_state(room_id=0):
    now = time.monotonic()
    return {'wage': 4900, 'target': 6000, 'focus_minutes': 20, 'focus_s': 1200,
            'white': 27, 'caught': 2, 'speed': 5.1, 'status': 'working',
            'work_left': 11, 'work_total': 25, 'work_progress': 0.56, 'work_reward': 100,
            'slack_left': 8, 'slack_total': 10, 'caught_left': 4, 'caught_total': 5,
            'next_day_left': 45, 'clock': '周三 17:14:42', 'room': str(room_id or ''),
            'viewer': {'name': '观众', 'text': '今天也要准时下班呀'},
            'speech': '先敲完这行代码，鲨总还在后面盯着呢。', 'speech_until': now + 60,
            'floaters': [{'text': '+100', 'x': 560, 'y': 143, 'at': now - 0.2, 'color': _GREEN}]}


def preview_jpeg(room_id=0):
    frame = WageRenderer(room_id).render(demo_state(room_id), time.monotonic())
    ok, encoded = cv2.imencode('.jpg', frame[..., :3], [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError('上班摸鱼画面预览生成失败')
    return encoded.tobytes()
