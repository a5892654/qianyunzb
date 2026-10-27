"""ttsd 客户端：连（必要时拉起）常驻的 Qwen3-TTS 流式服务，边收边播。

服务本身跑在独立 venv（C:\\gamedata\\ttsd\\.venv）里，用 faster-qwen3-tts 的
CUDA-graph 流式推理；鲸语这边只负责把它当成一条 PCM 流来读。
"""
import asyncio
import logging
import os
import subprocess

import aiohttp
import numpy as np

import procutil
from config import LOG_DIR

log = logging.getLogger("ttsd")

DEFAULT_BASE = "http://127.0.0.1:8899"
_proc = None


def _base(cfg_tts):
    return str(cfg_tts.get("ttsd_base") or DEFAULT_BASE).rstrip("/")


def _resolve_ref(cfg_tts):
    ref = str(cfg_tts.get("qwen_ref_audio") or "").strip().strip('"')
    if not ref:
        return ""
    if os.path.isabs(ref):
        return ref
    input_dir = os.path.join(str(cfg_tts.get("comfy_dir", r"C:\max\ComfyUI")), "input")
    return os.path.join(input_dir, ref)


async def alive(cfg_tts):
    try:
        # trust_env=False：本机跑 TUN 代理，别让「服务在不在」这种本地探测绕出去
        async with aiohttp.ClientSession(trust_env=False) as s:
            async with s.get(_base(cfg_tts) + "/health", timeout=aiohttp.ClientTimeout(total=3)) as r:
                if r.status != 200:
                    return False
                return bool((await r.json()).get("ok"))
    except Exception:
        return False


def spawn(cfg_tts, on_status=None):
    """拉起 ttsd 常驻进程；模型首次上卡要几十秒，调用方自己等 alive()。"""
    global _proc
    if _proc is not None and _proc.poll() is None:
        return True
    py = str(cfg_tts.get("ttsd_python", r"C:\gamedata\ttsd\.venv\Scripts\python.exe"))
    script = str(cfg_tts.get("ttsd_script", r"C:\gamedata\ttsd\ttsd.py"))
    if not os.path.exists(py) or not os.path.exists(script):
        if on_status:
            on_status(f"找不到流式语音引擎：{py} / {script}")
        return False
    ref = _resolve_ref(cfg_tts)
    if not ref or not os.path.exists(ref):
        if on_status:
            on_status(f"流式语音缺参考音频：{ref or '未填写'}")
        return False
    cmd = [py, script,
           "--model", str(cfg_tts.get("ttsd_model", r"C:\max\ComfyUI\models\Qwen3-TTS-Models\Qwen3-TTS-12Hz-1.7B-Base")),
           "--ref-audio", ref,
           "--language", str(cfg_tts.get("qwen_language", "Chinese")),
           "--chunk-size", str(int(cfg_tts.get("ttsd_chunk_size", 8))),
           "--port", _base(cfg_tts).rsplit(":", 1)[-1]]
    os.makedirs(LOG_DIR, exist_ok=True)
    logf = open(os.path.join(LOG_DIR, "ttsd.log"), "wb")
    env = procutil.clean_env({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "USE_TF": "0", "NO_PROXY": "*"})
    _proc = subprocess.Popen(cmd, cwd=os.path.dirname(script), stdout=logf, stderr=subprocess.STDOUT,
                             env=env, creationflags=procutil.CREATE_NO_WINDOW)
    logf.close()
    procutil.bind_to_app(_proc.pid)
    if on_status:
        on_status("正在启动流式语音引擎（模型常驻显存，首次约 40~90 秒）…")
    return True


def stop():
    global _proc
    if _proc is not None and _proc.poll() is None:
        procutil.kill_tree(_proc.pid)
    _proc = None


async def ensure(cfg_tts, on_status):
    """保证服务可用：已在跑就复用，没跑且允许自启就拉起并等它热起来。"""
    if await alive(cfg_tts):
        return True
    if not cfg_tts.get("ttsd_auto_start", True):
        return False
    if not spawn(cfg_tts, on_status):
        return False
    for _ in range(240):
        await asyncio.sleep(1)
        if _proc is not None and _proc.poll() is not None:
            on_status("流式语音引擎启动失败，看 logs/ttsd.log")
            return False
        if await alive(cfg_tts):
            on_status("流式 Qwen3-TTS 已就绪（边说边合成）")
            return True
    on_status("流式语音引擎启动超时")
    return False


async def synth_stream(text, cfg_tts, timeout_first: float = 25.0):
    """按到达顺序产出 (pcm int16 mono, sr)。

    服务端每块约 0.67 秒音频，TCP 会把它切成碎段，所以这里攒到约 0.2 秒
    再吐一次：既保住首块延迟，又避免播放器被碎片喂出咔哒声。
    """
    url = _base(cfg_tts) + "/say"
    body = {"text": text, "language": str(cfg_tts.get("qwen_language", "Chinese"))}
    # 只约束首块：后续块由服务端生成速度决定，设总超时会长句被掐断。
    to = aiohttp.ClientTimeout(total=None, connect=5, sock_read=timeout_first)
    buf = bytearray()
    floor = 0
    async with aiohttp.ClientSession(trust_env=False) as s:
        async with s.post(url, json=body, timeout=to) as r:
            if r.status != 200:
                raise RuntimeError(f"ttsd {r.status}: {(await r.text())[:200]}")
            sr = int(r.headers.get("X-Sample-Rate", "24000"))
            floor = max(2048, int(sr * 2 * 0.2))
            async for raw in r.content.iter_any():
                buf += raw
                if len(buf) >= floor:
                    take = bytes(buf[: len(buf) & ~1])
                    del buf[: len(buf) & ~1]
                    yield np.frombuffer(take, dtype="<i2").astype(np.int16).reshape(-1, 1), sr
    if buf:
        take = bytes(buf[: len(buf) & ~1])
        yield np.frombuffer(take, dtype="<i2").astype(np.int16).reshape(-1, 1), sr


def tail_log(n=80):
    try:
        with open(os.path.join(LOG_DIR, "ttsd.log"), "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""
