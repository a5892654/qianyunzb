import asyncio
import glob
import io
import logging
import os
import subprocess
import threading
import time
import wave

import aiohttp
import numpy as np
import sounddevice as sd

import duckaudio
import procutil
from config import LOG_DIR

log = logging.getLogger("tts")

PRETRAINED_GPT = [
    "GPT_SoVITS/pretrained_models/s1v3.ckpt",
    "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt",
    "GPT_SoVITS/pretrained_models/s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt",
]
PRETRAINED_SOVITS = [
    "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
    "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth",
    "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s2G2333k.pth",
    "GPT_SoVITS/pretrained_models/s2Gv3.pth",
    "GPT_SoVITS/pretrained_models/gsv-v4-pretrained/s2Gv4.pth",
]


def _abs(gsv_dir, p):
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(gsv_dir, p))


ENV_FPS = 30


def envelope(pcm, sr):
    """口型用的音量包络：每秒 30 帧，0~1。"""
    x = np.abs(pcm[:, 0].astype(np.float32))
    hop = max(1, sr // ENV_FPS)
    n = len(x) // hop
    if n == 0:
        return []
    e = x[: n * hop].reshape(n, hop).mean(axis=1)
    ref = float(np.percentile(e, 95)) or 1.0
    return [round(float(v), 2) for v in np.clip(e / ref, 0, 1)]


SAPI_SCRIPT = r"""
param([string]$Text, [string]$Out, [int]$Rate = 0, [int]$Pitch = 0)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$zh = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' } | Select-Object -First 1
if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) }
$s.Rate = $Rate
$s.SetOutputToWaveFile($Out)
# 音高只能走 SSML，且必须在 SetOutputToWaveFile 之后
if ($Pitch -ne 0) {
  $sign = if ($Pitch -gt 0) { "+" } else { "" }
  $safe = $Text.Replace("&", "&amp;").Replace("<", "&lt;")
  $ssml = "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'><prosody pitch='" + $sign + $Pitch + "%'>" + $safe + "</prosody></speak>"
  $s.SpeakSsml($ssml)
} else {
  $s.Speak($Text)
}
$s.Dispose()
"""

# 对齐大肥鱼那种偏亮的播报感。原素材 SSML 是 pitch +18% / rate +8%，
# 但 SAPI 的 Rate 是 -10..10 的档位，每档约 ±10%，所以 +8% 只对应 1 档。
SAPI_RATE = 1
SAPI_PITCH = 18


async def _sapi_synth(text: str, rate: int | None = None, pitch: int | None = None):
    """GPT-SoVITS 没配好时的兜底：用 Windows 自带语音合成，保证台词有声音。"""
    import tempfile
    r = SAPI_RATE if rate is None else rate
    pt = SAPI_PITCH if pitch is None else pitch
    out = os.path.join(tempfile.gettempdir(), f"wt_sapi_{int(time.time() * 1000)}.wav")
    script = os.path.join(tempfile.gettempdir(), "wt_sapi.ps1")
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(SAPI_SCRIPT)
    proc = await asyncio.create_subprocess_exec(
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
        "-Text", text, "-Out", out, "-Rate", str(r), "-Pitch", str(pt),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        creationflags=procutil.CREATE_NO_WINDOW)
    _, err = await asyncio.wait_for(proc.communicate(), 30)
    try:
        if proc.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(err.decode("utf-8", "replace")[:200] or "系统语音合成失败")
        with wave.open(out) as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if sw != 2:
            raise RuntimeError(f"不支持的采样位宽 {sw}")
        return np.frombuffer(raw, dtype=np.int16).reshape(-1, ch), sr
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


class GsvEngine:
    def __init__(self):
        self.proc = None
        self.comfy_proc = None
        self.applied = (None, None)
        self.lock = asyncio.Lock()
        self.log_path = os.path.join(LOG_DIR, "gsv_engine.log")

    @staticmethod
    def _session():
        return aiohttp.ClientSession(trust_env=False)

    async def alive(self, url):
        """True=在线, False=连不上, None=有监听但忙（合成中会阻塞 /control）。"""
        try:
            async with self._session() as s:
                async with s.get(url.rstrip("/") + "/control", timeout=aiohttp.ClientTimeout(total=3)):
                    return True
        except asyncio.TimeoutError:
            return None
        except Exception:
            self.applied = (None, None)
            return False

    def list_weights(self, gsv_dir):
        gpt, sovits = [], []
        for p in PRETRAINED_GPT:
            if os.path.exists(_abs(gsv_dir, p)):
                gpt.append(p)
        for p in PRETRAINED_SOVITS:
            if os.path.exists(_abs(gsv_dir, p)):
                sovits.append(p)
        for d in sorted(glob.glob(os.path.join(gsv_dir, "GPT_weights*"))):
            gpt += [os.path.relpath(f, gsv_dir).replace("\\", "/") for f in sorted(glob.glob(os.path.join(d, "*.ckpt")))]
        for d in sorted(glob.glob(os.path.join(gsv_dir, "SoVITS_weights*"))):
            sovits += [os.path.relpath(f, gsv_dir).replace("\\", "/") for f in sorted(glob.glob(os.path.join(d, "*.pth")))]
        return gpt, sovits

    def _start_comfyui(self, cfg_tts, on_status) -> bool:
        """拉起 ComfyUI。它的 Python 环境不在系统 PATH 里，要走专用解释器。"""
        comfy = str(cfg_tts.get("comfy_dir", r"C:\max\ComfyUI")).rstrip("\\/")
        main_py = os.path.join(comfy, "main.py")
        if not os.path.exists(main_py):
            on_status(f"找不到 ComfyUI：{main_py}")
            return False
        py = str(cfg_tts.get("comfy_python", "")).strip()
        if not py or not os.path.exists(py):
            for cand in (os.path.join(os.path.dirname(comfy), "python", "python.exe"),
                         os.path.join(comfy, "python_embeded", "python.exe"),
                         os.path.join(comfy, ".venv", "Scripts", "python.exe")):
                if os.path.exists(cand):
                    py = cand
                    break
        if not py:
            on_status("找不到 ComfyUI 的 Python，请在语音设置里指定 comfy_python")
            return False
        if self.comfy_proc is not None and self.comfy_proc.poll() is None:
            return True
        try:
            logf = open(os.path.join(LOG_DIR, "comfyui_engine.log"), "ab")
            # 这台 ComfyUI 的 tensorflow 装了但 distutils 缺失（Python 3.12 移除了），
            # transformers 一旦探测到 tf 就会在导入 AutoProcessor 时崩掉，
            # 连锁导致 qwen_tts 整个不可用。关掉 tf 探测走纯 torch 路线。
            env = dict(os.environ)
            env["USE_TF"] = "0"
            env["USE_TORCH"] = "1"
            self.comfy_proc = subprocess.Popen(
                [py, main_py, "--port", "8188", "--listen", "127.0.0.1"],
                cwd=comfy, stdout=logf, stderr=subprocess.STDOUT, env=env,
                creationflags=procutil.CREATE_NO_WINDOW)
            procutil.bind_to_app(self.comfy_proc.pid)
        except OSError as e:
            on_status(f"启动 ComfyUI 失败：{e}")
            return False
        on_status("正在启动 ComfyUI（Qwen3-TTS 需要它，首次加载模型较慢）…")
        return True

    async def start(self, cfg_tts, on_status):
        # 流式 ttsd 优先：模型常驻、边说边合成，不需要 ComfyUI 排队整句
        if cfg_tts.get("qwen_enabled") and cfg_tts.get("stream_enabled", True):
            from dtgame import ttsd
            if await ttsd.ensure(cfg_tts, on_status):
                return True
            on_status("流式语音引擎不可用，退回 ComfyUI 整句合成")

        # Qwen3-TTS 走 ComfyUI，优先处理
        if cfg_tts.get("qwen_enabled"):
            from dtgame import qwentts
            if await qwentts.alive(cfg_tts):
                if await qwentts.has_nodes(cfg_tts):
                    on_status("Qwen3-TTS 已就绪（音色：%s）" % cfg_tts.get("qwen_ref_audio", ""))
                    return True
                on_status("ComfyUI 在跑，但没装 ComfyUI-TD-Qwen3TTS 节点")
            elif cfg_tts.get("comfy_auto_start", True):
                ok = self._start_comfyui(cfg_tts, on_status)
                if ok:
                    for _ in range(60):
                        await asyncio.sleep(1)
                        if await qwentts.alive(cfg_tts):
                            if await qwentts.has_nodes(cfg_tts):
                                on_status("Qwen3-TTS 已就绪（音色：%s）" % cfg_tts.get("qwen_ref_audio", ""))
                                return True
                            on_status("ComfyUI 起来了，但没装 ComfyUI-TD-Qwen3TTS 节点")
                            break
                    else:
                        on_status("ComfyUI 启动超时，先用系统语音兜底")
            else:
                on_status("ComfyUI 未运行，先用系统语音兜底")

        url = cfg_tts["api_url"]
        if await self.alive(url) is not False:
            return True
        gsv = cfg_tts["gsv_dir"]
        if not gsv:
            if cfg_tts.get("fallback_sapi", True):
                on_status("没配置 GPT-SoVITS，暂用 Windows 系统语音。想换成自己的音色：语音设置 → 选 GPT-SoVITS 目录 + 参考音频")
            else:
                on_status("还没有设置 GPT-SoVITS 目录：请在【语音设置】里选择 GPT-SoVITS 所在的文件夹")
            return False
        py = os.path.join(gsv, "runtime", "python.exe")
        if not os.path.exists(py) or not os.path.exists(os.path.join(gsv, "api_v2.py")):
            on_status(f"GPT-SoVITS 目录无效：{gsv}")
            return False
        if self.proc is None or self.proc.poll() is not None:
            hostport = url.split("://", 1)[-1].rstrip("/")
            host, _, port = hostport.partition(":")
            os.makedirs(LOG_DIR, exist_ok=True)
            logf = open(self.log_path, "w", encoding="utf-8")
            env = procutil.clean_env({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_PROXY": "*"})
            self.proc = subprocess.Popen(
                [py, "api_v2.py", "-a", host or "127.0.0.1", "-p", port or "9880",
                 "-c", "GPT_SoVITS/configs/tts_infer.yaml"],
                cwd=gsv, stdout=logf, stderr=subprocess.STDOUT, env=env,
                creationflags=procutil.CREATE_NO_WINDOW)
            logf.close()
            if cfg_tts.get("stop_on_exit", True):
                procutil.bind_to_app(self.proc.pid)
            on_status("正在启动 GPT-SoVITS 引擎（首次约 20~60 秒）…")
        for _ in range(180):
            await asyncio.sleep(1)
            if self.proc and self.proc.poll() is not None:
                on_status("GPT-SoVITS 引擎启动失败，请查看 logs/gsv_engine.log")
                return False
            if await self.alive(url):
                on_status("GPT-SoVITS 引擎已就绪")
                return True
        on_status("GPT-SoVITS 引擎启动超时")
        return False

    def stop(self, keep_comfyui: bool = False):
        if self.proc and self.proc.poll() is None:
            procutil.kill_tree(self.proc.pid)
        self.proc = None
        if not keep_comfyui and self.comfy_proc and self.comfy_proc.poll() is None:
            procutil.kill_tree(self.comfy_proc.pid)
            self.comfy_proc = None
        self.applied = (None, None)

    def tail_log(self, n=60):
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except OSError:
            return ""

    async def _apply_weights(self, s, cfg_tts):
        want = (cfg_tts["gpt_weights"], cfg_tts["sovits_weights"])
        if want == self.applied:
            return
        base = cfg_tts["api_url"].rstrip("/")
        gsv = cfg_tts["gsv_dir"]
        for ep, path in (("set_gpt_weights", want[0]), ("set_sovits_weights", want[1])):
            if not path:
                continue
            async with s.get(f"{base}/{ep}", params={"weights_path": _abs(gsv, path)},
                             timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status != 200:
                    raise RuntimeError(f"切换权重失败 {ep}: {(await r.text())[:200]}")
        self.applied = want

    async def synth(self, cfg_tts, text):
        # 引擎优先级：ComfyUI/Qwen3-TTS 克隆 → GPT-SoVITS → 系统语音兜底
        if cfg_tts.get("qwen_enabled"):
            try:
                from dtgame import qwentts
                data = await qwentts.synth(text, cfg_tts)
                return _decode_wav(data)
            except Exception as e:
                log.warning("Qwen3-TTS 合成失败，往下退：%s", e)
                if not cfg_tts.get("fallback_sapi", True) and not cfg_tts.get("gsv_dir"):
                    raise
        ref = cfg_tts.get("ref_audio", "").strip().strip('"')
        if not ref or not os.path.exists(ref):
            if cfg_tts.get("fallback_sapi", True):
                return await _sapi_synth(
                    text,
                    rate=int(cfg_tts.get("sapi_rate", SAPI_RATE)),
                    pitch=int(cfg_tts.get("sapi_pitch", SAPI_PITCH)),
                )
            raise RuntimeError("还没有设置参考音频（语音设置 → 参考音频）")
        body = {
            "text": text, "text_lang": cfg_tts.get("text_lang", "zh"),
            "ref_audio_path": os.path.abspath(ref), "prompt_text": cfg_tts.get("ref_text", ""),
            "prompt_lang": cfg_tts.get("ref_lang", "zh"), "text_split_method": "cut5",
            "batch_size": 1, "media_type": "wav", "streaming_mode": False,
            "speed_factor": float(cfg_tts.get("speed", 1.0)), "temperature": float(cfg_tts.get("temperature", 1.0)),
            "top_k": int(cfg_tts.get("top_k", 15)), "top_p": float(cfg_tts.get("top_p", 1.0)),
            "sample_steps": int(cfg_tts.get("sample_steps", 32)), "repetition_penalty": 1.35,
        }
        async with self.lock:
            async with self._session() as s:
                await self._apply_weights(s, cfg_tts)
                async with s.post(cfg_tts["api_url"].rstrip("/") + "/tts", json=body,
                                  timeout=aiohttp.ClientTimeout(total=180)) as r:
                    data = await r.read()
                    if r.status != 200:
                        raise RuntimeError(f"合成失败 HTTP {r.status}: {data[:300].decode('utf-8', 'replace')}")
        with wave.open(io.BytesIO(data)) as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if sw != 2:
            raise RuntimeError(f"不支持的采样位宽 {sw}")
        pcm = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch)
        return pcm, sr


def decode_audio_file(filename):
    """读取准备好的 AI 翻唱或音效，不经过 TTS 生成。"""
    import miniaudio
    decoded = miniaudio.decode_file(str(filename), output_format=miniaudio.SampleFormat.SIGNED16)
    return np.frombuffer(decoded.samples, dtype=np.int16).reshape(-1, decoded.nchannels), decoded.sample_rate


def _decode_wav(data: bytes):
    """音频字节 → (pcm int16, 采样率)。ComfyUI 的 SaveAudio 出的是 flac，也兼容 wav。"""
    if data[:4] == b"fLaC":
        import miniaudio
        dec = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16)
        pcm = np.frombuffer(dec.samples, dtype=np.int16).reshape(-1, dec.nchannels)
        return pcm, dec.sample_rate
    with wave.open(io.BytesIO(data)) as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise RuntimeError(f"不支持的采样位宽 {sw}")
    return np.frombuffer(raw, dtype=np.int16).reshape(-1, ch), sr


def _resample(x, sr_from, sr_to):
    """x: (n, ch)。线性插值重采样，语音/音乐够用。"""
    if sr_from == sr_to or len(x) == 0:
        return x
    n = int(round(len(x) * sr_to / sr_from))
    src = np.arange(len(x), dtype=np.float64)
    dst = np.linspace(0, len(x) - 1, n)
    return np.stack([np.interp(dst, src, x[:, c]) for c in range(x.shape[1])], axis=1).astype(x.dtype)


def _stereo(x):
    return np.repeat(x, 2, axis=1) if x.shape[1] == 1 else x[:, :2]


class Player:
    """单路输出混音器：一条常驻音频流，同时放背景音乐和语音；说话时自动压低音乐。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.open_lock = threading.Lock()
        self.stream = None
        self.dev_name = None
        self.sr = 48000
        self.voice = None
        self.vpos = 0
        self.vheard = 0
        self.vstart = None
        self._lat_cache = None
        self.voice_vol = 0.8
        self.voice_done = threading.Event()
        self.voice_done.set()
        self.streaming = False
        self._abort = threading.Event()
        self.duck_pids = set()
        self.duck_level = 1.0
        self.music = None
        self.mpos = 0
        self.music_paused = False
        self.music_vol = 0.6
        self.duck = 0.3
        self.mgain = 1.0
        self.on_music_end = None

    @staticmethod
    def devices():
        out = []
        apis = sd.query_hostapis()
        for d in sd.query_devices():
            if d["max_output_channels"] > 0:
                out.append(f"{d['name']} [{apis[d['hostapi']]['name']}]")
        return out

    @staticmethod
    def _resolve(name):
        if not name:
            return None
        apis = sd.query_hostapis()
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and f"{d['name']} [{apis[d['hostapi']]['name']}]" == name:
                return i
        return None

    def ensure(self, device_name):
        with self.open_lock:
            self._ensure(device_name)

    def _ensure(self, device_name):
        with self.lock:
            if self.stream is not None and self.stream.active and device_name == self.dev_name:
                return
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        idx = self._resolve(device_name)
        info = sd.query_devices(idx) if idx is not None else sd.query_devices(kind="output")
        sr = int(info["default_samplerate"]) or 48000
        with self.lock:
            if sr != self.sr:
                if self.music is not None:
                    pos = self.mpos
                    self.music = _resample(self.music, self.sr, sr)
                    self.mpos = int(pos * sr / self.sr)
                if self.voice is not None:
                    self.voice = None
                    self.voice_done.set()
            self.sr = sr
        self.stream = sd.OutputStream(samplerate=sr, channels=2, dtype="float32", device=idx,
                                      callback=self._callback, latency="high")
        self.stream.start()
        self._lat_cache = None
        self.dev_name = device_name

    def _callback(self, outdata, frames, _time, _status):
        out = np.zeros((frames, 2), np.float32)
        ended = None
        with self.lock:
            target = self.duck if self.voice is not None else 1.0
            g0 = self.mgain
            g1 = g0 + (target - g0) * min(1.0, frames / (self.sr * 0.25))
            if self.music is not None and not self.music_paused:
                n = min(frames, len(self.music) - self.mpos)
                ramp = np.linspace(g0, g1, n, dtype=np.float32)[:, None]
                out[:n] += self.music[self.mpos:self.mpos + n].astype(np.float32) * (ramp * (self.music_vol / 32768.0))
                self.mpos += n
                if self.mpos >= len(self.music):
                    self.music = None
                    ended, self.on_music_end = self.on_music_end, None
            self.mgain = g1
            if self.voice is not None:
                n = min(frames, len(self.voice) - self.vpos)
                out[:n] += self.voice[self.vpos:self.vpos + n] * self.voice_vol
                self.vpos += n
                # feed_stream 会把已播掉的部分丢掉并重置 vpos，所以口型要另计累计量。
                # 缓冲区被抽空时 n=0，这个钟也会停住——正是我们想要的：声音卡住嘴也别往前跑。
                self.vheard += n
                # 流式时缓冲区被抽空只是「暂时没到货」，不算播完；真正收尾由 end_stream 决定。
                if self.vpos >= len(self.voice) and not self.streaming:
                    self.voice = None
                    self.voice_done.set()
        np.clip(out, -1, 1, out=out)
        outdata[:] = out
        if ended:
            ended()

    # ---------- 语音 ----------
    def set_duck(self, pids, level):
        """说话期间要压低的进程（一般是游戏）和压到多响。"""
        with self.lock:
            self.duck_pids = set(pids or ())
            self.duck_level = max(0.0, min(1.0, level))

    def _setup_voice(self, device_name, volume):
        self.ensure(device_name)
        self._abort.clear()
        with self.lock:
            self.voice_vol = max(0, min(200, volume)) / 100.0
            self.voice_done.clear()
            self.vheard = 0
            self.vstart = None
            pids, level = self.duck_pids, self.duck_level
        if pids and level < 1.0:
            duckaudio.duck(pids, level)

    def voice_clock(self):
        """此刻真正在响的语音位置（秒）。口型照它对齐：
        按"消息发出的墙上时间"推会先天抢跑——首块音频要等 TTS，声卡缓冲还有 latency 那一截。
        起播时 PortAudio 会连着回调把缓冲灌满，vheard 瞬间跳前约一个 latency，
        所以再用"不超过起播以来经过的时间"兜住，嘴就不会在开头抢跑。"""
        with self.lock:
            heard = self.vheard / self.sr - self._out_latency_cached()
            now = time.monotonic()
            if heard > 0 and self.vstart is None:
                self.vstart = now
            if self.vstart is not None:
                heard = min(heard, now - self.vstart)
        return max(0.0, heard)

    def _out_latency_cached(self):
        """输出延迟取一次就缓存：它是设备属性，别在混音回调锁里现问 PortAudio。"""
        if self._lat_cache is None:
            self._lat_cache = self._out_latency()
        return self._lat_cache

    def _out_latency(self):
        try:
            lat = self.stream.latency if self.stream is not None else 0.0
        except Exception:
            return 0.0
        return float(lat[1] if isinstance(lat, tuple) else lat)

    def _wait_done(self):
        while not self.voice_done.wait(0.05):
            if self._abort.is_set():
                with self.lock:
                    self.voice, self.streaming = None, False
                    self.voice_done.set()
        if self.duck_pids:
            duckaudio.restore(self.duck_pids)

    def play_blocking(self, pcm, sr, device_name, volume):
        self._setup_voice(device_name, volume)
        data = _stereo(_resample(pcm.astype(np.float32) / 32768.0, sr, self.sr))
        with self.lock:
            self.streaming = False
            self.voice, self.vpos = data, 0
        self._wait_done()

    # ---------- 流式语音：首块到就能开口 ----------
    def begin_stream(self, device_name, volume):
        self._setup_voice(device_name, volume)
        with self.lock:
            self.streaming = True
            self.voice, self.vpos = np.zeros((0, 2), np.float32), 0

    def feed_stream(self, pcm, sr):
        """追加一段音频。已播掉的部分顺手丢掉，缓冲区不会随着句子变长而膨胀。"""
        data = _stereo(_resample(pcm.astype(np.float32) / 32768.0, sr, self.sr))
        with self.lock:
            if self.voice is None or self._abort.is_set():
                return
            self.voice = np.concatenate((self.voice[self.vpos:], data))
            self.vpos = 0

    def end_stream(self):
        """生成结束：此后播到缓冲区尾部才算真的说完。"""
        with self.lock:
            self.streaming = False
            if self.voice is not None and self.vpos >= len(self.voice):
                self.voice = None
                self.voice_done.set()
        self._wait_done()

    def skip(self):
        self._abort.set()

    @property
    def playing(self):
        return not self.voice_done.is_set()

    # ---------- 音乐 ----------
    def decode_music(self, data, device_name):
        import miniaudio
        self.ensure(device_name)
        sr = self.sr
        d = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=2, sample_rate=sr)
        return np.frombuffer(d.samples, dtype=np.int16).reshape(-1, 2), sr

    def play_music(self, pcm, sr, device_name, on_end):
        self.ensure(device_name)
        if sr != self.sr:
            pcm = _resample(pcm, sr, self.sr)
        with self.lock:
            self.music, self.mpos, self.music_paused = pcm, 0, False
            self.on_music_end = on_end

    def stop_music(self):
        with self.lock:
            self.music = None
            self.on_music_end = None

    def pause_music(self, paused):
        with self.lock:
            self.music_paused = paused

    def set_music(self, volume, duck):
        with self.lock:
            self.music_vol = max(0, min(150, volume)) / 100.0
            self.duck = max(0, min(100, duck)) / 100.0

    def music_progress(self):
        with self.lock:
            if self.music is None:
                return 0, 0
            return self.mpos / self.sr, len(self.music) / self.sr

    def close(self):
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None

