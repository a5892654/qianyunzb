"""离线生成鲨总原创胡言乱语音效；Windows 中文 SAPI + 确定性音效合成。"""
from pathlib import Path
import base64
import json
import subprocess
import tempfile
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'web/assets/wage-sfx'
SR = 48000
LINES = {
    'inspect': ('查岗探头', '歪比？巴布？', 3, -24),
    'scold': ('抓包训话', '歪比巴布！哇啦巴巴！', 5, -30),
    'assign': ('派离谱需求', '巴布，歪比歪比！', 4, -22),
    'angry': ('鲨总发怒', '哇啦哇啦！巴布！', 5, -35),
    'approve': ('批准夸奖', '歪比！巴布！', 4, -17),
}


def speak(text, rate, pitch, path):
    encoded = base64.b64encode(text.encode('utf-8')).decode('ascii')
    target = str(path).replace("'", "''")
    script = f"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$s=New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{
  $zh=$s.GetInstalledVoices() | Where-Object {{ $_.VoiceInfo.Culture.Name -like 'zh*' }} | Select-Object -First 1
  if (!$zh) {{ throw '需要 Windows 中文语音才能重建音效' }}
  $s.SelectVoice($zh.VoiceInfo.Name)
  $s.Rate={rate}
  $s.SetOutputToWaveFile('{target}')
  $text=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
  $escaped=[Security.SecurityElement]::Escape($text)
  $s.SpeakSsml('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="zh-CN"><prosody pitch="{pitch}%">'+$escaped+'</prosody></speak>')
}} finally {{ $s.Dispose() }}
"""
    result = subprocess.run(['powershell', '-NoProfile', '-Command', script],
                            creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace'))
    with wave.open(str(path)) as f:
        if f.getsampwidth() != 2:
            raise ValueError('SAPI 输出必须是 16 位 PCM')
        data = np.frombuffer(f.readframes(f.getnframes()), '<i2').reshape(-1, f.getnchannels()).mean(axis=1) / 32768
        src_sr = f.getframerate()
    audible = np.flatnonzero(np.abs(data) > .004)
    if not len(audible):
        raise ValueError('SAPI 输出为空')
    data = data[max(0, audible[0] - src_sr // 30):min(len(data), audible[-1] + src_sr // 12)]
    # 低沉、滑稽的忽快忽慢；不是肥鱼的 TTS，也不依赖在线服务。
    t = np.arange(round(len(data) / src_sr * SR / .86)) / SR
    speed = .86 + .075 * np.sin(2 * np.pi * 6.3 * t)
    positions = np.cumsum(speed) * src_sr / SR
    result = np.interp(positions[positions < len(data)], np.arange(len(data)), data)
    result = np.tanh(result * 2.3) * (.88 + .12 * np.sin(2 * np.pi * 21 * np.arange(len(result)) / SR))
    return result / max(.01, np.max(np.abs(result))) * .68


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / 'outputs/wage-sfx-build'
    scratch.mkdir(parents=True, exist_ok=True)
    manifest = {'provenance': 'Original nonsense phrases synthesized with local Windows Chinese SAPI; pitch wobble and original procedural Foley. No sampled game/meme audio.', 'sounds': {}}
    rng = np.random.default_rng(7928)
    for name, (label, text, rate, pitch) in LINES.items():
        with tempfile.TemporaryDirectory(prefix='wage-sfx-', dir=scratch) as tmp:
            voice = speak(text, rate, pitch, Path(tmp) / 'voice.wav')
        duration = min(3.0, len(voice) / SR + .44)
        data = np.zeros(round(duration * SR))

        def add(sound, start, gain=1):
            at = round(start * SR)
            count = min(len(sound), len(data) - at)
            if count > 0:
                data[at:at + count] += sound[:count] * gain

        def tone(frequency, seconds, decay=8, slide=0):
            t = np.arange(round(seconds * SR)) / SR
            return np.sin(2 * np.pi * (frequency * t + slide * t * t / 2)) * np.exp(-decay * t) * np.minimum(1, t / .006)

        add(voice, .18)
        if name == 'inspect':
            add(tone(85, .16, 24), .01, .35)
            add(tone(73, .15, 24), .13, .25)
            add(tone(700, .22, 14, -1800), .02, .12)
        elif name == 'scold':
            add(tone(170, .42, 12, -240), 0, .32)
        elif name == 'assign':
            rustle = rng.normal(0, 1, round(.18 * SR))
            rustle -= np.roll(rustle, 4)
            rustle *= np.sin(np.linspace(0, np.pi, len(rustle))) ** 2
            add(rustle, .01, .035)
            add(tone(420, .19, 18, 800), .05, .16)
        elif name == 'angry':
            add(tone(130, .32, 13) + tone(139, .32, 13), .01, .18)
        else:
            add(tone(1046.5, .36, 12), .01, .16)
            add(tone(1568, .45, 12), .13, .16)
        fade = min(480, len(data) // 2)
        data[:fade] *= np.linspace(0, 1, fade)
        data[-fade:] *= np.linspace(1, 0, fade)
        data *= .78 / max(.78, np.max(np.abs(data)))
        pcm = (data * 32767).astype('<i2')
        path = OUT / f'{name}.wav'
        with wave.open(str(path), 'wb') as f:
            f.setparams((1, 2, SR, 0, 'NONE', 'not compressed'))
            f.writeframes(pcm.tobytes())
        manifest['sounds'][name] = {'label': label, 'phrase': text, 'seconds': round(duration, 3), 'peak': round(float(np.max(np.abs(data))), 3)}
    (OUT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    build()
