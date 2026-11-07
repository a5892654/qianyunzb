"""ttsd 客户端：连（必要时拉起）常驻的 Qwen3-TTS 流式服务，边收边播。

服务本身跑在独立 venv（C:\\gamedata\\ttsd\\.venv）里，用 faster-qwen3-tts 的
CUDA-graph 流式推理；鲸语这边只负责把它当成一条 PCM 流来读。
"""
import asyncio
import logging
import os
import subprocess
import time

import aiohttp
import numpy as np

import procutil
from config import LOG_DIR, BASE_DIR

log = logging.getLogger("ttsd")

DEFAULT_BASE = "http://127.0.0.1:8899"
_proc = None
_signature = None
_generation = 0
_ensure_lock = asyncio.Lock()


def owned():
    return _proc is not None and _proc.poll() is None


def owned_model():
    return _signature[0] if owned() and _signature else ""


def runtime_paths(cfg_tts):
    py = str(cfg_tts.get("ttsd_python", ""))
    script = str(cfg_tts.get("ttsd_script", ""))
    if not os.path.isfile(py):
        py = os.path.join(BASE_DIR, ".tts-venv", "Scripts", "python.exe")
    if not os.path.isfile(script):
        script = os.path.join(BASE_DIR, "tools", "tts_server.py")
    return py, script


def _desired(cfg):
    return (str(cfg.get("ttsd_model", "Qwen/Qwen3-TTS-12Hz-0.6B-Base")),
            _resolve_ref(cfg), str(cfg.get("qwen_language", "Chinese")), int(cfg.get("ttsd_chunk_size", 8)))


def _base(cfg_tts):
    return str(cfg_tts.get("ttsd_base") or DEFAULT_BASE).rstrip("/")


def _resolve_ref(cfg_tts):
    ref = str(cfg_tts.get("qwen_ref_audio") or "").strip().strip('"')
    if not ref:
        return ""
    if os.path.isabs(ref):
        return ref
    candidates = [os.path.join(str(cfg_tts.get("comfy_dir", r"C:\max\ComfyUI")), "input", ref),
                  os.path.join(BASE_DIR, "input", ref), os.path.join(BASE_DIR, ref)]
    return next((path for path in candidates if os.path.isfile(path)), candidates[0])


async def health(cfg_tts):
    try:
        # trust_env=False：本机跑 TUN 代理，别让「服务在不在」这种本地探测绕出去
        async with aiohttp.ClientSession(trust_env=False) as s:
            async with s.get(_base(cfg_tts) + "/health", timeout=aiohttp.ClientTimeout(total=3)) as r:
                if r.status != 200:
                    return {}
                data = await r.json()
                return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def alive(cfg_tts):
    data = await health(cfg_tts)
    if not data.get("ok"):
        return False
    if owned() and _signature != _desired(cfg_tts):
        return False
    actual = str(data.get("model", "")).replace("\\", "/").rstrip("/").split("/")[-1]
    desired = _desired(cfg_tts)[0].replace("\\", "/").rstrip("/").split("/")[-1]
    return actual == desired if actual else owned() and _signature == _desired(cfg_tts)


def _run_bytes(argv, timeout):
    """跑系统命令拿字节。不能用 text=True：中文 Windows 的默认代码页不是 UTF-8，
    解码失败会让 stdout 变成 None（PowerShell 读线程直接抛 UnicodeDecodeError），
    后面的 splitlines 就炸——引擎永远起不来，表现为直播间彻底没声。"""
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout,
                              creationflags=procutil.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or b"").decode("utf-8", "replace")


def _listening_pids(port):
    """占着端口的进程。残留服务会一直监听，新实例拉起来会撞端口。"""
    out = _run_bytes(["netstat", "-ano", "-p", "tcp"], 10)
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        if not parts[1].endswith(":%d" % port):
            continue
        try:
            pids.add(int(parts[4]))
        except ValueError:
            continue
    return pids


def _ttsd_pids(script):
    """命令行里跑着这个 ttsd 脚本的 python 进程（含 uv 再拉起的子进程）。"""
    needle = os.path.basename(script).lower()
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
           "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains('%s') } | "
           "ForEach-Object { $_.ProcessId }" % needle)
    out = _run_bytes(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], 20)
    pids = set()
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.add(int(line))
    return pids


def owns_port(cfg_tts) -> bool:
    """端口占用者是不是同脚本拉起的 ttsd（本软件上次被强杀后遗留的）。"""
    try:
        port = int(_base(cfg_tts).rsplit(":", 1)[-1])
    except ValueError:
        port = 8899
    script = str(cfg_tts.get("ttsd_script", r"C:\gamedata\ttsd\ttsd.py"))
    return bool(_listening_pids(port) & _ttsd_pids(script))


def kill_stale(cfg_tts, on_status=None):
    """拉起前清掉残留的 ttsd：每份模型常驻约 4.8GB 显存，
    端口不冲突时完全看不出来，直到新实例加载时把显卡挤爆（表现为引擎无声消失）。

    清理是尽力而为：扫描或杀进程失败都只记日志，绝不能拦住引擎启动。
    """
    script = str(cfg_tts.get("ttsd_script", r"C:\gamedata\ttsd\ttsd.py"))
    try:
        port = int(_base(cfg_tts).rsplit(":", 1)[-1])
    except ValueError:
        port = 8899
    mine = {os.getpid()}
    if _proc is not None and _proc.poll() is None:
        mine.add(_proc.pid)
    try:
        found = _listening_pids(port) | _ttsd_pids(script)
    except Exception as e:
        log.warning("残留 ttsd 扫描失败（不影响启动）：%s", e)
        found = set()
    stale = sorted(p for p in found if p not in mine)
    killed = []
    for pid in stale:
        try:
            procutil.kill_tree(pid)
            killed.append(pid)
        except Exception as e:
            log.warning("清理残留 pid=%s 失败（继续启动）：%s", pid, e)
    stale = killed
    if stale:
        log.warning("清理残留 ttsd 进程：%s", stale)
        if on_status:
            on_status("已清理残留的流式语音进程：%s" % ", ".join(str(p) for p in stale))
    return stale


def spawn(cfg_tts, on_status=None):
    """拉起 ttsd 常驻进程；模型首次上卡要几十秒，调用方自己等 alive()。"""
    global _proc, _signature, _generation
    if _proc is not None and _proc.poll() is None:
        return True
    kill_stale(cfg_tts, on_status)
    py, script = runtime_paths(cfg_tts)
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
           "--model", _desired(cfg_tts)[0],
           "--ref-audio", ref,
           "--language", str(cfg_tts.get("qwen_language", "Chinese")),
           "--chunk-size", str(int(cfg_tts.get("ttsd_chunk_size", 8))),
           "--port", _base(cfg_tts).rsplit(":", 1)[-1]]
    os.makedirs(LOG_DIR, exist_ok=True)
    logf = open(os.path.join(LOG_DIR, "ttsd.log"), "wb")
    env = procutil.clean_env({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "USE_TF": "0", "NO_PROXY": "127.0.0.1,localhost"})
    _proc = subprocess.Popen(cmd, cwd=os.path.dirname(script), stdout=logf, stderr=subprocess.STDOUT,
                             env=env, creationflags=procutil.CREATE_NO_WINDOW)
    _signature = _desired(cfg_tts)
    _generation += 1
    logf.close()
    procutil.bind_to_app(_proc.pid)
    if on_status:
        on_status("正在启动流式语音引擎（模型常驻显存，首次约 40~90 秒）…")
    return True


def stop():
    global _proc, _signature, _generation
    _generation += 1
    if _proc is not None and _proc.poll() is None:
        procutil.kill_tree(_proc.pid)
    _proc = None
    _signature = None


async def ensure(cfg_tts, on_status):
    settings = dict(cfg_tts)
    async with _ensure_lock:
        return await _ensure(settings, on_status)


async def _ensure(cfg_tts, on_status):
    """保证服务可用：已在跑就复用，没跑且允许自启就拉起并等它热起来。"""
    generation = _generation
    if owned() and _signature != _desired(cfg_tts):
        on_status("正在释放旧语音模型显存并切换模型…")
        await asyncio.to_thread(stop)
        generation = _generation
    state = await health(cfg_tts)
    if generation != _generation:
        raise asyncio.CancelledError
    if state.get("ok") and await alive(cfg_tts):
        return True
    if state and not owned():
        if not await asyncio.to_thread(owns_port, cfg_tts):
            on_status("语音端口由外部引擎占用，无法核对或切换模型；请先关闭该外部语音服务")
            return False
        on_status("正在接管上次遗留的语音引擎并切换到所选模型…")
        await asyncio.to_thread(kill_stale, cfg_tts, on_status)
    if not cfg_tts.get("ttsd_auto_start", True):
        return False
    if not spawn(cfg_tts, on_status):
        return False
    generation = _generation
    deadline = time.monotonic() + 240
    download_deadline = time.monotonic() + 3600
    last_phase = ""
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        if generation != _generation:
            raise asyncio.CancelledError
        if _proc is not None and _proc.poll() is not None:
            on_status("流式语音引擎启动失败，看 logs/ttsd.log")
            return False
        state = await health(cfg_tts)
        if generation != _generation:
            raise asyncio.CancelledError
        if state.get("error"):
            on_status("流式语音加载失败：" + str(state["error"]))
            await asyncio.to_thread(stop)
            return False
        phase = state.get("phase", "")
        if phase == "downloading":
            deadline = download_deadline
            if last_phase != phase:
                on_status("首次使用正在下载语音模型，请等待；可在语音设置中关闭引擎取消")
        elif last_phase == "downloading":
            deadline = time.monotonic() + 240
        last_phase = phase
        if await alive(cfg_tts):
            on_status("流式 Qwen3-TTS 已就绪（边说边合成）")
            return True
    on_status("流式语音引擎启动超时")
    await asyncio.to_thread(stop)
    return False


async def synth_stream(text, cfg_tts, timeout_first: float = 25.0):
    """按到达顺序产出 (pcm int16 mono, sr)。

    服务端每块约 0.67 秒音频，TCP 会把它切成碎段，所以这里攒到约 0.2 秒
    再吐一次：既保住首块延迟，又避免播放器被碎片喂出咔哒声。
    """
    if not await alive(cfg_tts):
        raise RuntimeError("流式语音未就绪或与所选模型不一致，请在语音设置中切换或启动引擎")
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
