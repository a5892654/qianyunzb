"""使用已有皮套帧生成办公室各玩法的合成样图；不读取或修改真实进度。"""
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
from live_overlay import OverlayCompositor
from live_wage import WageRenderer
from wage import WageEngine, DEFAULTS, avatar_layout

OUTPUT = Path(__file__).resolve().parents[1] / 'outputs' / 'wage-preview'


def main():
    default_avatar = cv2.imread(str(OUTPUT / 'avatar.png'), cv2.IMREAD_UNCHANGED)
    if default_avatar is None:
        raise RuntimeError('先运行 tools/preview_wage_model.cjs 生成皮套帧')
    for scene in ('rescue', 'task', 'rules', 'items', 'promotion', 'debt'):
        engine = WageEngine({**DEFAULTS, 'auto_slack': False, 'catch_rate': 0})
        engine.state.update(wage=4900, focus_s=1200, white=27, caught=2, speed=5.1)
        engine.state['career'].update(xp=850, wallet=12)
        def say(text, uid=42):
            engine.on_event({'type': 'danmaku', 'uid': uid, 'name': '观众', 'text': text})
        if scene == 'rescue':
            say('摸鱼'); say('老板来了'); say('装开会'); say('疯狂打字', 43); say('打小报告', 44)
        elif scene == 'task':
            say('鲨总派活'); say('1'); say('2', 43)
        elif scene == 'rules':
            say('改员工守则'); say('2')
            engine.play._finish_round()
        elif scene == 'items':
            for item in ('咖啡', '挡箭牌', '坏键盘'):
                say('兑换' + item); say('使用' + item)
        elif scene == 'promotion':
            engine.play._grant_xp(80)
        elif scene == 'debt':
            engine.state['wage'] = -500
            engine.play.on_balance()
            engine.state['status'] = 'caught'
        renderer = WageRenderer()
        snapshot = engine.snapshot()
        avatar = cv2.imread(str(OUTPUT / f"avatar-{snapshot['status']}.png"), cv2.IMREAD_UNCHANGED)
        if avatar is None:
            avatar = default_avatar
        compositor = OverlayCompositor(SimpleNamespace(frame=lambda: avatar),
                                       lambda: avatar_layout(DEFAULTS, snapshot))
        frame = renderer.render(snapshot, foreground=False)
        compositor.apply(frame)
        renderer.apply_overlays(frame, snapshot, time.monotonic())
        path = OUTPUT / f'office-{scene}.png'
        cv2.imwrite(str(path), frame)
        print(path.name)


if __name__ == '__main__':
    main()
