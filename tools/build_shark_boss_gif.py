"""把生图生成的单张透明鲨总做成连续 GIF；不调用模型、不生成宫格。"""
from pathlib import Path
import math

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'web' / 'assets'
SIZE = (512, 600)


def build_asset(filename, variants):
    source = Image.open(ASSETS / filename).convert('RGBA')
    source.thumbnail((454, 536), Image.Resampling.LANCZOS)
    poster = Image.new('RGBA', SIZE)
    poster.alpha_composite(source, ((SIZE[0] - source.width) // 2, SIZE[1] - source.height - 20))
    rgba = np.asarray(poster).astype(np.float32)
    rgba[..., :3] *= rgba[..., 3:4] / 255
    yy, xx = np.mgrid[:SIZE[1], :SIZE[0]].astype(np.float32)
    x, y = xx / SIZE[0], yy / SIZE[1]
    head = np.exp(-((x - .57) / .21) ** 4 - ((y - .20) / .13) ** 4)
    arm = np.exp(-((x - .28) / .22) ** 4 - ((y - .31) / .18) ** 4)
    jaw = np.exp(-((x - .57) / .14) ** 4 - ((y - .24) / .03) ** 4)
    # 一套公共色板；透明索引单独保留，避免黑西装和墨镜被误设为透明。
    palette = poster.convert('RGB').quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    palette_colors = [0, 0, 0] + palette.getpalette()[:765]

    for name, count, duration, scolding in variants:
        frames = []
        for index in range(count):
            phase = index / count * math.tau
            breath = .5 - .5 * math.cos(phase)
            jab = max(0, math.sin(phase * 2)) ** 6 if scolding else 0
            nod = math.sin(phase * (2 if scolding else 1))
            zoom = 1 + (.028 if scolding else .009) * breath
            map_x = (xx - SIZE[0] * .54) / zoom + SIZE[0] * .54
            map_y = (yy - SIZE[1] * .80) / zoom + SIZE[1] * .80
            map_x += arm * jab * 9 + head * nod * (1.4 if scolding else .4)
            map_y -= head * nod * (2.8 if scolding else 1.2)
            if scolding:
                map_y -= jaw * (max(0, math.sin(phase * 6)) ** 2) * 2.8
            warped = cv2.remap(rgba, map_x.astype(np.float32), map_y.astype(np.float32),
                               cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT)
            alpha = np.clip(warped[..., 3:4], 0, 255)
            rgb = warped[..., :3] * 255 / np.maximum(alpha, 1)
            frame = Image.fromarray(np.uint8(np.clip(np.concatenate((rgb, alpha), axis=2), 0, 255)))
            indexed = np.asarray(frame.convert('RGB').quantize(palette=palette, dither=Image.Dither.NONE))
            indices = indexed.astype(np.uint16) + 1
            indices[np.asarray(frame)[..., 3] < 128] = 0
            box = Image.fromarray(np.uint8(indices > 0) * 255).getbbox()
            if not box or box[0] <= 4 or box[1] <= 4 or box[2] >= SIZE[0] - 4 or box[3] >= SIZE[1] - 4:
                raise ValueError(f'{name}: frame {index} clips the full-body silhouette')
            gif_frame = Image.fromarray(indices.astype(np.uint8)).convert('P')
            gif_frame.putpalette(palette_colors)
            frames.append(gif_frame)
        frames[0].save(ASSETS / name, save_all=True, append_images=frames[1:],
                       duration=duration, loop=0, transparency=0, disposal=2, optimize=False)
        with Image.open(ASSETS / name) as result:
            print(f'{name}: {result.n_frames} frames, {duration} ms/frame, transparent, loops forever')


def build():
    build_asset('shark-boss-fullbody.png', (
        ('shark-boss-fullbody.gif', 48, 60, True),
        ('shark-boss-fullbody-idle.gif', 32, 100, False)))
    for pose in ('inspect', 'assign', 'angry', 'approve'):
        build_asset(f'shark-boss-{pose}.png', (
            (f'shark-boss-{pose}.gif', 32, 80, pose == 'angry'),))


if __name__ == '__main__':
    build()
