"""用已验收的真实 Live2D 帧合成办公室与五种鲨总音效预览，不连接账号。"""
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import wave

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live_capture import FFMPEG_PATH
from live_overlay import OverlayCompositor
from live_wage import WageRenderer
from wage import WageEngine, DEFAULTS, avatar_layout
from wage_audio import load_sound


def main():
    output = ROOT / 'outputs/wage-premium'
    output.mkdir(parents=True, exist_ok=True)
    observed = ROOT / 'outputs/wage-animation'
    samples = json.loads((observed / 'samples.json').read_text(encoding='utf-8'))
    working = [(name, sample) for name, sample in zip(samples['frames'], samples['samples']) if sample['label'] == 'working']
    caught = [(name, sample) for name, sample in zip(samples['frames'], samples['samples']) if sample['label'] == 'caught']
    moods = ('inspect', 'scold', 'assign', 'angry', 'approve')
    sr, fps, segment = 48000, 12, 3
    audio = np.zeros(sr * segment * len(moods), np.int16)
    for i, mood in enumerate(moods):
        pcm, _ = load_sound(mood)
        start = round((i * segment + .25) * sr)
        audio[start:start + len(pcm)] = (pcm[:, 0].astype(float) * .7).astype(np.int16)
    wav_path = output / 'shark-sfx-demo.wav'
    with wave.open(str(wav_path), 'wb') as f:
        f.setparams((1, 2, sr, 0, 'NONE', 'not compressed'))
        f.writeframes(audio.tobytes())
    clip = output / 'office-premium-sfx.mp4'
    process = subprocess.Popen([FFMPEG_PATH, '-n', '-loglevel', 'error', '-f', 'rawvideo',
        '-pix_fmt', 'bgr24', '-s', '1280x720', '-r', str(fps), '-i', '-', '-i', str(wav_path),
        '-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '20', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '128k', '-shortest', '-movflags', '+faststart', str(clip)],
        stdin=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    engine = WageEngine(DEFAULTS)
    engine.state.update(wage=4900, focus_s=1200, white=27, caught=2, speed=1)
    engine.state['career'].update(xp=850, wallet=12)
    state = engine.snapshot()
    state.update(viewer=None, speech='', floaters=[])
    renderer = WageRenderer()
    avatar = None
    compositor = OverlayCompositor(SimpleNamespace(frame=lambda: avatar), lambda: avatar_layout(DEFAULTS, state))
    timings = []
    try:
        for i, mood in enumerate(moods):
            frames = caught if mood in ('scold', 'angry') else working
            state['status'] = 'caught' if mood in ('scold', 'angry') else 'working'
            state['office'].update(boss_pose=mood)
            state['clock'] = '周四 17:14:42'
            for frame_index in range(segment * fps):
                name, sample = frames[frame_index % len(frames)]
                avatar = cv2.imread(str(observed / name), cv2.IMREAD_UNCHANGED)
                if avatar is None:
                    raise RuntimeError('缺少真实皮套验收帧：' + name)
                state['focus_s'] = sample.get('workTime', 0)
                started = time.perf_counter()
                frame = renderer.render(state, foreground=False)
                compositor.apply(frame)
                renderer.apply_overlays(frame, state, i * segment + frame_index / fps + 100)
                timings.append((time.perf_counter() - started) * 1000)
                if frame_index == 12:
                    cv2.imwrite(str(output / f'office-{mood}.png'), frame)
                process.stdin.write(cv2.resize(frame[..., :3], (1280, 720)).tobytes())
    finally:
        process.stdin.close()
    if process.wait(timeout=30):
        raise RuntimeError('视频编码失败')
    capture = cv2.VideoCapture(str(clip))
    assert capture.isOpened()
    assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == len(moods) * segment * fps
    capture.release()
    print(json.dumps({'video': str(clip), 'audio': str(wav_path), 'frames': len(timings),
                      'composition_p50_ms': round(float(np.percentile(timings, 50)), 1),
                      'composition_p95_ms': round(float(np.percentile(timings, 95)), 1)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
