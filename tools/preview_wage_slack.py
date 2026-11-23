"""用实际皮套验收帧合成摸鱼预览；不访问直播账号、语音或真实存档。"""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live_capture import FFMPEG_PATH
from live_overlay import OverlayCompositor
from live_wage import WageRenderer
from wage import WageEngine, DEFAULTS, avatar_layout
from wage_slack import SLACK_EVENTS


def main():
    output = ROOT / 'outputs/wage-slack'
    output.mkdir(parents=True, exist_ok=True)
    observed = ROOT / 'outputs/wage-animation'
    samples = json.loads((observed / 'samples.json').read_text(encoding='utf-8'))
    engine = WageEngine({**DEFAULTS, 'auto_slack': False, 'tasks_enabled': False, 'catch_rate': 0})
    engine.state.update(wage=4900, focus_s=1200, white=27, caught=2, speed=1)
    renderer = WageRenderer()
    avatar = None
    state = {}
    compositor = OverlayCompositor(SimpleNamespace(frame=lambda: avatar), lambda: avatar_layout(DEFAULTS, state))
    sheet = Image.new('RGB', (1680, ((len(SLACK_EVENTS) + 1) // 2) * 464), (9, 28, 49))
    sheet_draw = ImageDraw.Draw(sheet)
    clip = output / 'slack-events.mp4'
    process = subprocess.Popen([FFMPEG_PATH, '-n', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
        '-s', '1280x720', '-r', '10', '-i', '-', '-an', '-c:v', 'h264_nvenc', '-preset', 'p4',
        '-cq', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(clip)],
        stdin=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    count = 0
    try:
        for index, event in enumerate(SLACK_EVENTS):
            engine._start_slack('working', event['id'])
            state = engine.snapshot()
            state.update(speech='', viewer=None, floaters=[], clock='周四 17:14:42')
            rows = [(name, sample) for name, sample in zip(samples['frames'], samples['samples'])
                    if sample['label'] == 'slack-' + event['id']]
            if not rows:
                raise RuntimeError('先运行 tools/verify_wage_animation.cjs 生成全部情景的真实皮套帧')
            for j, (name, _) in enumerate(rows):
                avatar = cv2.imread(str(observed / name), cv2.IMREAD_UNCHANGED)
                if avatar is None:
                    raise RuntimeError('皮套验收帧丢失：' + name)
                state.update(slack_elapsed=j/10, slack_left=engine._slack_total-j/10)
                frame = renderer.render(state, foreground=False)
                compositor.apply(frame)
                renderer.apply_overlays(frame, state, 100 + count / 10)
                process.stdin.write(cv2.resize(frame[..., :3], (1280, 720)).tobytes())
                count += 1
            cv2.imwrite(str(output / (event['id'] + '.png')), frame)
            crop = cv2.cvtColor(frame[350:980, 20:1280, :3], cv2.COLOR_BGR2RGB)
            tile = Image.fromarray(cv2.resize(crop, (840, 420)))
            x, y = (index % 2) * 840, (index // 2) * 464
            sheet_draw.text((x+22, y+6), event['title'], font=renderer.font(26, True), fill=event['accent'])
            sheet.paste(tile, (x, y+44))
            engine._finish_slack(False)
    finally:
        process.stdin.close()
    if process.wait(timeout=30):
        raise RuntimeError('预览视频编码失败')
    sheet.save(output / 'slack-contact-sheet.jpg', quality=92)
    capture = cv2.VideoCapture(str(clip))
    assert capture.isOpened() and int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == count
    capture.release()
    print(json.dumps({'video': str(clip), 'frames': count, 'sheet': str(output / 'slack-contact-sheet.jpg')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
