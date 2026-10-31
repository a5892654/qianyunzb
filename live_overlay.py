"""把 Live2D 皮套渲染成带透明通道的 RGBA 帧，供直播画面叠加使用。

用无头 Edge 打开 /live2d 页面，通过 CDP 读取 WebGL 画布的透明 PNG。
直接读取画布可保留 alpha，不依赖浏览器截屏是否输出透明通道。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import tempfile
import threading
import time

import cv2
import numpy as np

from procutil import bind_to_app

_EDGE_CANDIDATES = ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")
_MAX_EDGE_MS = 40000
_BLANK_GRACE_S = 8.0
_FRAME_INTERVAL_S = 1 / 30


class OverlayError(RuntimeError):
    """面向用户的错误信息。"""


def find_edge() -> str | None:
    for var in _EDGE_CANDIDATES:
        base = os.environ.get(var)
        if not base:
            continue
        path = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.isfile(path):
            return path
    for base in (r"C:\Program Files (x86)", r"C:\Program Files"):
        path = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.isfile(path):
            return path
    return None


def _decode_png(raw: bytes) -> np.ndarray | None:
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim != 3 or image.shape[2] != 4:
        return None
    return image


class Live2DOverlay:
    """后台线程把 Live2D 渲染成 RGBA 帧；调用方只取最新一帧。"""

    def __init__(self, render_settings=None) -> None:
        self._render_settings = render_settings
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_at = 0.0
        self._error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._ready = threading.Event()
        self._viewport = 720
        self.width = 0
        self.height = 0

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._error)

    def start(self, url: str, box: int, timeout: float = 45.0) -> None:
        if self.alive:
            return
        if self._thread:
            self.stop()
        edge = find_edge()
        if not edge:
            raise OverlayError("找不到 Microsoft Edge，无法渲染 Live2D 皮套")
        self._url = url
        self._viewport = int(box)
        self._stop.clear()
        self._ready.clear()
        with self._lock:
            self._frame, self._error, self._frame_at = None, "", 0.0
        self._thread = threading.Thread(target=self._run, args=(edge,), name="l2d-overlay", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            err = self.error
            self.stop()
            raise OverlayError(err or "Live2D 皮套渲染超时")
        err = self.error
        if err:
            self.stop()
            raise OverlayError(err)

    def frame(self, max_age: float = 1.0) -> np.ndarray | None:
        with self._lock:
            frame, at = self._frame, self._frame_at
        if frame is None or time.monotonic() - at > max_age:
            return None
        return frame


    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None
        self._proc = None
        with self._lock:
            self._frame = None

    # ---------------- 内部线程 ----------------

    def _run(self, edge: str) -> None:
        try:
            asyncio.run(self._serve(edge))
        except OverlayError as exc:
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - 线程里必须兜住，否则错误丢失
            self._fail(f"Live2D 叠加层异常：{exc}")

    def _fail(self, msg: str) -> None:
        with self._lock:
            if not self._error:
                self._error = msg
        self._ready.set()

    async def _serve(self, edge: str) -> None:
        port = 9340 + (os.getpid() % 400)
        profile = os.path.join(tempfile.gettempdir(), f"qianyin-l2d-{os.getpid()}")
        proc = subprocess.Popen(
            [edge, "--headless=new", f"--remote-debugging-port={port}",
             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
             "--use-angle=default", "--enable-gpu", "--disable-gpu-vsync",
             "--disable-features=CalculateNativeWinOcclusion,Translate",
             "--mute-audio",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._proc = proc
        try:
            bind_to_app(proc.pid)
        except Exception:
            pass
        try:
            ws_url = await self._wait_target(port, proc)
            if not ws_url:
                raise OverlayError("Live2D 渲染窗口没有响应")
            await self._capture_frames(ws_url)
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass

    async def _wait_target(self, port: int, proc: subprocess.Popen) -> str | None:
        import aiohttp

        deadline = time.monotonic() + _MAX_EDGE_MS / 1000
        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline and not self._stop.is_set():
                if proc.poll() is not None:
                    return None
                try:
                    async with session.get(f"http://127.0.0.1:{port}/json", timeout=2) as resp:
                        for target in await resp.json():
                            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                                return target["webSocketDebuggerUrl"]
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
                await asyncio.sleep(0.4)
        return None

    async def _capture_frames(self, ws_url: str) -> None:
        import aiohttp

        width = height = self._viewport
        msg_id = 0

        async def command(ws, method: str, params: dict | None = None):
            nonlocal msg_id
            msg_id += 1
            await ws.send_str(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            async with asyncio.timeout(5):
                while not self._stop.is_set():
                    msg = await ws.receive()
                    if msg.type is not aiohttp.WSMsgType.TEXT:
                        raise OverlayError("Live2D 渲染进程已断开")
                    data = json.loads(msg.data)
                    if data.get("id") != msg_id:
                        continue
                    if "error" in data:
                        raise OverlayError("Live2D 渲染命令失败：" + data["error"].get("message", method))
                    return data.get("result", {})
            return {}

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, max_msg_size=32 * 1024 * 1024,
                                          heartbeat=10) as ws:
                await command(ws, "Page.enable")
                await command(ws, "Emulation.setDeviceMetricsOverride",
                           {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
                await command(ws, "Emulation.setDefaultBackgroundColorOverride",
                           {"color": {"r": 0, "g": 0, "b": 0, "a": 0}})
                await command(ws, "Page.navigate", {"url": self._url})
                deadline = time.monotonic() + _MAX_EDGE_MS / 1000
                blank_since = None
                while not self._stop.is_set():
                    started = time.monotonic()
                    view = self._render_settings() if self._render_settings else {}
                    result = await command(ws, "Runtime.evaluate", {
                        "expression": "window.captureLive2DFrame ? window.captureLive2DFrame("
                                      + json.dumps(view) + ") : (window.live2dOverlayState || {})",
                        "returnByValue": True,
                    })
                    if result.get("exceptionDetails"):
                        raise OverlayError("Live2D 画布读取失败，请检查模型与渲染库")
                    value = result.get("result", {}).get("value") or {}
                    if value.get("error"):
                        raise OverlayError(str(value["error"]))
                    png = value.get("png")
                    if png:
                        image = _decode_png(base64.b64decode(png))
                        if image is None:
                            raise OverlayError("Live2D 画布没有输出透明 PNG")
                        if not np.any(image[..., 3] > 8):
                            blank_since = blank_since or started
                            if started - blank_since > _BLANK_GRACE_S:
                                raise OverlayError("Live2D 画面为空白，请在皮套页调整取景或检查模型")
                        else:
                            blank_since = None
                            # 保留整张画布，避免首次包围盒裁掉后续动作和位置变化。
                            with self._lock:
                                self._frame, self._frame_at = image, time.monotonic()
                                self.height, self.width = image.shape[:2]
                            self._ready.set()
                    elif time.monotonic() > deadline:
                        raise OverlayError("Live2D 模型加载超时，请检查模型文件和渲染库")
                    await asyncio.sleep(max(0.001, _FRAME_INTERVAL_S - (time.monotonic() - started)))


class OverlayCompositor:
    """把皮套帧按位置/大小叠到游戏画面上。"""

    def __init__(self, overlay: Live2DOverlay, settings) -> None:
        self.overlay = overlay
        self.settings = settings
        self._box = (0, 0, 0, 0)

    def _resize(self, layer: np.ndarray, target_h: int) -> np.ndarray:
        scale = target_h / layer.shape[0]
        target_w = max(1, int(round(layer.shape[1] * scale)))
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        scaled = cv2.resize(layer, (target_w, target_h), interpolation=interp)
        alpha = scaled[..., 3]
        rows = np.flatnonzero(alpha.max(axis=1) > 0)
        cols = np.flatnonzero(alpha.max(axis=0) > 0)
        self._box = ((int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)
                     if rows.size and cols.size else (0, 0, target_w, target_h))
        return scaled

    def apply(self, frame: np.ndarray) -> None:
        layer = self.overlay.frame()
        if layer is None:
            return
        settings = self.settings()
        if not settings:
            return
        position, size, margin, *coords = settings
        height, width = frame.shape[:2]
        target_h = max(48, min(height * 2, int(round(height * float(size)))))
        layer = self._resize(layer, target_h)
        lh, lw = layer.shape[:2]
        pad = int(round(height * float(margin)))
        x0 = pad if "l" in position else width - lw - pad
        y0 = pad if "t" in position else height - lh - pad
        if coords and coords[0] is not None:
            x0 = int(round(width * coords[0] - lw / 2))
        if len(coords) > 1 and coords[1] is not None:
            y0 = int(round(height * coords[1] - lh / 2))
        bx0, by0, bx1, by1 = self._box
        cx0, cy0 = max(0, x0 + bx0), max(0, y0 + by0)
        cx1, cy1 = min(width, x0 + bx1), min(height, y0 + by1)
        if cx1 <= cx0 or cy1 <= cy0:
            return
        src = layer[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
        dst = frame[cy0:cy1, cx0:cx1]
        alpha = src[..., 3]
        # 只算有人像的像素，避免整块缩放后的透明区域也参与浮点运算
        ys, xs = np.nonzero(alpha)
        if not ys.size:
            return
        y_from, y_to = ys.min(), ys.max() + 1
        x_from, x_to = xs.min(), xs.max() + 1
        a = alpha[y_from:y_to, x_from:x_to].astype(np.float32) * (1.0 / 255.0)
        a = a[..., None]
        patch_dst = dst[y_from:y_to, x_from:x_to, :3]
        patch_src = src[y_from:y_to, x_from:x_to, :3].astype(np.float32)
        patch_dst[::] = (patch_src * a + patch_dst.astype(np.float32) * (1.0 - a)).astype(np.uint8)
