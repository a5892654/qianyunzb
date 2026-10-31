"""Qwen3-TTS 语音合成（走本地 ComfyUI）。

和 GPT-SoVITS 是两条独立路径：
  - GPT-SoVITS 走 api_v2.py 的 /tts
  - 这条走 ComfyUI 的 /prompt + /history，用在 ComfyUI-TD-Qwen3TTS 节点里
音色靠参考音频克隆（x_vector_only_mode，不需要参考文本）。
"""

import asyncio
import json
import logging
import os
import uuid

import aiohttp

log = logging.getLogger("qwentts")

DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"


def build_prompt(text: str, cfg: dict, node_id: str) -> dict:
    """组装 ComfyUI 工作流。四个节点：加载模型 → 读参考音频 → 克隆合成 → 存盘。"""
    return {
        "1": {"class_type": "TDQwen3TTSModelLoader", "inputs": {
            "model_path": cfg.get("qwen_model", DEFAULT_MODEL),
            "precision": cfg.get("qwen_precision", "bf16"),
            "device": cfg.get("qwen_device", "cuda"),
            "attn_implementation": "sdpa",
            "auto_download": False,
            "download_source": "ModelScope",
        }},
        "2": {"class_type": "LoadAudio", "inputs": {
            "audio": cfg.get("qwen_ref_audio", "dafeiyu_voice_ref.wav"),
        }},
        "3": {"class_type": "TDQwen3TTSVoiceClone", "inputs": {
            "model": ["1", 0],
            "text": text,
            "ref_audio": ["2", 0],
            "language": cfg.get("qwen_language", "Chinese"),
            "ref_text": "",
            "x_vector_only_mode": True,
        }},
        "4": {"class_type": "SaveAudio", "inputs": {
            "audio": ["3", 0],
            "filename_prefix": f"whaletalk_{node_id}",
        }},
    }


async def synth(text: str, cfg: dict, timeout: float = 180.0) -> bytes:
    """合成一段语音，返回 wav 字节。"""
    base = str(cfg.get("qwen_base", "http://127.0.0.1:8188")).rstrip("/")
    node_id = uuid.uuid4().hex[:12]
    payload = {"prompt": build_prompt(text, cfg, node_id), "client_id": uuid.uuid4().hex}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        try:
            async with s.post(f"{base}/prompt", json=payload) as r:
                if r.status != 200:
                    raise RuntimeError(f"ComfyUI 拒绝任务 HTTP {r.status}: {(await r.text())[:200]}")
                pid = (await r.json())["prompt_id"]
        except aiohttp.ClientConnectorError as e:
            raise RuntimeError(f"连不上 ComfyUI {base}——它启动了吗？") from e

        # 轮询任务结果。间隔短一点，否则每句都要多等半秒到一秒。
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.12)
            async with s.get(f"{base}/history/{pid}") as r:
                if r.status != 200:
                    continue
                hist = await r.json()
            if pid not in hist:
                continue
            entry = hist[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"Qwen3-TTS 合成失败：{json.dumps(status, ensure_ascii=False)[:300]}")
            audio = _pick_audio(entry)
            if audio:
                return await _download(s, base, audio)
    raise RuntimeError(f"Qwen3-TTS 合成超时（{int(timeout)}s）")


def _pick_audio(entry: dict) -> dict | None:
    for node in (entry.get("outputs") or {}).values():
        for a in node.get("audio") or []:
            if a.get("filename"):
                return a
    return None


async def _download(s: aiohttp.ClientSession, base: str, audio: dict) -> bytes:
    params = {"filename": audio["filename"], "type": "output"}
    if audio.get("subfolder"):
        params["subfolder"] = audio["subfolder"]
    async with s.get(f"{base}/view", params=params) as r:
        if r.status != 200:
            raise RuntimeError(f"取音频失败 HTTP {r.status}")
        return await r.read()


async def alive(cfg: dict) -> bool:
    base = str(cfg.get("qwen_base", "http://127.0.0.1:8188")).rstrip("/")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
            async with s.get(f"{base}/system_stats") as r:
                return r.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def has_nodes(cfg: dict) -> bool:
    """确认 ComfyUI 里装了 Qwen3TTS 节点，避免任务提交后才失败。"""
    base = str(cfg.get("qwen_base", "http://127.0.0.1:8188")).rstrip("/")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(f"{base}/object_info/TDQwen3TTSVoiceClone") as r:
                return r.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


def find_comfyui(exe_hint: str = "") -> str:
    """找 ComfyUI 的启动入口，用于自动拉起。"""
    if exe_hint and os.path.exists(exe_hint):
        return exe_hint
    for p in (r"C:\max\ComfyUI\main.py", r"C:\ComfyUI\main.py"):
        if os.path.exists(p):
            return p
    for base in (r"C:\max\ComfyUI", r"C:\ComfyUI"):
        py = os.path.join(base, "python_embeded", "python.exe")
        if os.path.exists(py):
            return py + "|" + os.path.join(base, "main.py")
    return ""
