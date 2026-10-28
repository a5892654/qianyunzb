"""选定窗口与输出设备的本机预览、直播编码。"""
from __future__ import annotations

import collections
import ctypes
import os
import queue
import re
import socket
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

import cv2
import numpy as np
import pyaudiowpatch as pa

try:
    import windows_capture
except Exception:  # pragma: no cover - 依赖缺失时接口仍可读
    windows_capture = None

from procutil import bind_to_app as _bind_to_app

FFMPEG_PATH = r"C:/rg/ffmpeg.exe"
# 直播姬画布默认 1920x1080；我们只出 720p 的话它还得再放大一次，等于糊上加糊。
OUT_W, OUT_H, FPS = 1920, 1080, 30
# 预览/源画面走 MJPEG，q70 的块状糊在 1080p 全屏后很明显；这里每帧只多 1ms 左右。
JPEG_QUALITY = 82
_MIN_W, _MIN_H = 160, 90
_FRAME_WAIT_S = 8.0
_ENC_READY_S = 15.0
_AUDIO_CHUNK = 1024

_user32 = ctypes.windll.user32
_user32.GetDesktopWindow.restype = wintypes.HWND
_user32.GetShellWindow.restype = wintypes.HWND
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.BOOL]
_user32.MoveWindow.restype = wintypes.BOOL
_user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_WM_CLOSE = 0x0010
_GWL_STYLE = -16
_WS_CAPTION, _WS_THICKFRAME, _WS_BORDER, _WS_DLGFRAME = 0x00C00000, 0x00040000, 0x00800000, 0x00400000
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOZORDER, _SWP_NOACTIVATE, _SWP_FRAMECHANGED = 0x1, 0x2, 0x8, 0x20, 0x20
_GW_OWNER = 4


def _client_crop(hwnd: int) -> dict | None:
    if not _user32.IsWindow(wintypes.HWND(hwnd)):
        return None
    win, client, point = wintypes.RECT(), wintypes.RECT(), wintypes.POINT(0, 0)
    if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(win)):
        return None
    if not _user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(client)):
        return None
    if not _user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(point)):
        return None
    win_w, win_h = win.right - win.left, win.bottom - win.top
    client_w, client_h = client.right - client.left, client.bottom - client.top
    if win_w <= 0 or win_h <= 0 or client_w <= 0 or client_h <= 0:
        return None
    left, top = point.x - win.left, point.y - win.top
    if left < 0 or top < 0 or left + client_w > win_w or top + client_h > win_h:
        return None
    return {"left": left / win_w, "top": top / win_h,
            "width": client_w / win_w, "height": client_h / win_h,
            "window_aspect": win_w / win_h, "client_aspect": client_w / client_h}


def close_window(hwnd: int) -> None:
    """发 WM_CLOSE 让窗口自己关。源画面那个进程会 re-exec，terminate 只杀得到外壳。"""
    _user32.PostMessageW(wintypes.HWND(hwnd), _WM_CLOSE, 0, 0)


def client_size(hwnd: int) -> tuple[int, int]:
    r = wintypes.RECT()
    if not _user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return (0, 0)
    return (max(0, r.right - r.left), max(0, r.bottom - r.top))


def strip_caption(hwnd: int) -> None:
    """去掉标题条和可调边框。

    窗口采集（WGC）采的是整个窗口——实测采集帧尺寸等于 DWM 的扩展边框矩形，比客户区
    多了标题条那几十像素，所以源画面窗口不改无框的话，直播姬会把「千音源画面」那条
    标题栏一起播出去。
    """
    h = wintypes.HWND(hwnd)
    frame = _WS_CAPTION | _WS_THICKFRAME | _WS_BORDER | _WS_DLGFRAME
    style = _user32.GetWindowLongPtrW(h, _GWL_STYLE)
    if not style & frame:
        return
    _user32.SetWindowLongPtrW(h, _GWL_STYLE, style & ~frame)
    _user32.SetWindowPos(h, None, 0, 0, 0, 0, _SWP_FRAMECHANGED | _SWP_NOMOVE
                         | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)


def fit_borderless_client(hwnd: int, w: int, h: int) -> tuple[int, int]:
    """把窗口变成一块干净的采集画布：先去掉标题条，再把客户区闭环校正到 w x h。"""
    strip_caption(hwnd)
    return fit_client(hwnd, w, h)


def fit_client(hwnd: int, w: int, h: int, tries: int = 4) -> tuple[int, int]:
    """把窗口客户区闭环校正到 w x h（进程坐标系），返回调整后的客户区尺寸。

    浏览器 --window-size 要的是逻辑像素，150% 缩放时按 1920 请求会拿到 2880 再被屏幕
    钳掉，画面先被网页拉伸、再被直播姬缩小，观众看到的就是糊。边框宽度算不准（DPI
    虚拟化下每次换算都有零头），所以改成量一次、把差值补上、再量，直到对上。
    MoveWindow 实测比 SetWindowPos 可靠（后者对 Edge 窗口返回成功但尺寸不动）。
    """
    for _ in range(tries):
        cw, ch = client_size(hwnd)
        if (cw, ch) == (w, h):
            return (cw, ch)
        win = wintypes.RECT()
        if not cw or not ch or not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(win)):
            return (0, 0)
        _user32.MoveWindow(wintypes.HWND(hwnd), win.left, win.top,
                           (win.right - win.left) + (w - cw),
                           (win.bottom - win.top) + (h - ch), True)
    return client_size(hwnd)

_forbidden_classes = {"Progman", "WorkerW", "Shell_TrayWnd",
                      "Windows.UI.Core.CoreWindow", "Button"}
# 自动默认只认游戏窗口；这些标题绝不自动选（允许用户手选）。
_exclude_auto = ("千音", "qianyin", "qoder", "chrome", "edge", "firefox",
                 "浏览器", "quark", "sogouexplorer", "opera")


class CaptureError(RuntimeError):
    """面向用户的安全错误。"""


def _safe(msg: str) -> str:
    return re.sub(r"(tcp|rtmp|rtmps|http|https)://\S+", "<redacted>", str(msg))


def _get_title(hwnd_h) -> str:
    """SendMessageTimeout 取标题：目标线程挂起/不处理消息时超时放弃，不阻塞。"""
    buf = ctypes.create_unicode_buffer(512)
    res = wintypes.LPARAM()
    ok = _user32.SendMessageTimeoutW(
        hwnd_h, 0x000D, wintypes.WPARAM(512),
        ctypes.c_void_p(ctypes.addressof(buf)),
        0x0002 | 0x0001,  # SMTO_ABORTIFHUNG | SMTO_BLOCK
        wintypes.UINT(100), ctypes.byref(res))
    return buf.value if ok else ""


def _hwnd_info(hwnd: int, want_title: bool = True) -> dict | None:
    if not _user32.IsWindow(wintypes.HWND(hwnd)):
        return None
    title = _get_title(wintypes.HWND(hwnd)) if want_title else ""
    cls = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(wintypes.HWND(hwnd), cls, 256)
    rect = wintypes.RECT()
    _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return {"hwnd": hwnd, "title": title, "class": cls.value,
            "pid": int(pid.value),
            "width": max(0, rect.right - rect.left),
            "height": max(0, rect.bottom - rect.top),
            "visible": bool(_user32.IsWindowVisible(wintypes.HWND(hwnd))),
            "iconic": bool(_user32.IsIconic(wintypes.HWND(hwnd))),
            "owner": bool(_user32.GetWindow(wintypes.HWND(hwnd), _GW_OWNER))}


def _list_windows() -> list[dict]:
    out: list[dict] = []
    desktop = int(_user32.GetDesktopWindow() or 0)
    shell = int(_user32.GetShellWindow() or 0)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        hwnd = int(h)
        if hwnd in (desktop, shell):
            return True
        try:
            info = _hwnd_info(hwnd)
        except Exception:
            return True
        if (info and info["visible"] and not info["owner"]
                and info["class"] not in _forbidden_classes
                and info["title"] and info["width"] >= _MIN_W
                and info["height"] >= _MIN_H):
            out.append({**{k: info[k] for k in
                           ("hwnd", "title", "pid", "width", "height")},
                        "minimized": bool(info["iconic"])})
        return True

    _user32.EnumWindows(cb, 0)
    return out


def _pick_default_window(windows: list[dict]) -> int:
    for w in windows:
        if w.get("minimized"):
            continue
        t = w["title"].lower()
        if any(x in t for x in _exclude_auto):
            continue
        if "demontower" in t or ("godot" in t and "debug" in t):
            return w["hwnd"]
    return 0


def _list_loopback_devices() -> tuple[list[dict], int]:
    p = pa.PyAudio()
    devices: list[dict] = []
    default = 0
    try:
        for d in p.get_loopback_device_info_generator():
            devices.append({"id": int(d["index"]), "name": str(d["name"])})
        try:
            dl = p.get_default_wasapi_loopback()
            if int(dl["index"]) not in {x["id"] for x in devices}:
                devices.append({"id": int(dl["index"]),
                                "name": str(dl["name"])})
            default = int(dl["index"])
        except Exception:
            default = devices[0]["id"] if devices else 0
    finally:
        p.terminate()
    if not devices:
        default = 0
    return devices, default


def sources() -> dict:
    """枚举采集源；任何失败以 error 字段返回，不抛异常。"""
    result = {"windows": [], "audio": [], "default_window": 0,
              "default_audio": 0,
              "ffmpeg_ready": bool(os.path.isfile(FFMPEG_PATH)), "error": ""}
    if windows_capture is None:
        result["error"] = "windows-capture 未安装"
        return result
    try:
        result["windows"] = _list_windows()
        result["default_window"] = _pick_default_window(result["windows"])
    except Exception as exc:
        result["error"] = _safe(f"窗口枚举失败: {exc}")
    try:
        devs, dflt = _list_loopback_devices()
        result["audio"], result["default_audio"] = devs, dflt
    except Exception as exc:
        result["error"] = _safe((result["error"] + " " if result["error"]
                                 else "") + f"音频枚举失败: {exc}")
    return result


def _validate_window(hwnd: int) -> None:
    """启动/运行期共用：hwnd 必须是真实可见非最小化顶层窗口。"""
    if hwnd in (0, int(_user32.GetDesktopWindow() or 0),
               int(_user32.GetShellWindow() or 0)):
        raise CaptureError("不允许采集桌面或 shell 窗口")
    info = _hwnd_info(hwnd, want_title=False)
    if info is None:
        raise CaptureError("窗口句柄无效或已关闭")
    if info["class"] in _forbidden_classes:
        raise CaptureError("不允许采集系统桌面窗口")
    if not info["visible"]:
        raise CaptureError("目标窗口当前不可见")
    if info["iconic"]:
        raise CaptureError("目标窗口已最小化")
    if info["width"] < _MIN_W or info["height"] < _MIN_H:
        raise CaptureError("目标窗口尺寸过小")


class WindowStreamCapture:
    """单窗口预览 + 本地 ffmpeg FLV 输出。所有方法线程安全、stop 幂等。"""

    def __init__(self) -> None:
        self.hwnd: int = 0
        self.audio_device: int = 0
        self._crop: dict | None = None
        self.previewing = False
        self.output_running = False
        self.error = ""
        self.audio_level = 0.0
        self.started_at: float | None = None
        self.latest = 0.0  # 最新帧时间戳(time.monotonic)
        self.encoded_frames = 0  # ffmpeg 已编码视频帧数（进度解析）
        self._lock = threading.RLock()
        self._frame_cv = threading.Condition(self._lock)
        self._stop_evt = threading.Event()
        self._enc_evt = threading.Event()
        self._capture_closed = threading.Event()
        self._frame: bytes | None = None          # OUT_W x OUT_H BGRA
        self._source_frame: np.ndarray | None = None  # 最新游戏画面，合成时复制，避免残影
        self._raw_size: tuple[int, int] | None = None  # 采集帧原始尺寸（物理像素）
        self._mean = 0.0
        self._capture_times = collections.deque(maxlen=180)
        self._render_times = collections.deque(maxlen=180)
        self._process_times = collections.deque(maxlen=60)
        self._jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._jpeg_at = 0.0
        self._jpeg_thread: threading.Thread | None = None
        self._audio_q: queue.Queue = queue.Queue(maxsize=96)
        self._control = None
        self._pa: pa.PyAudio | None = None
        self._stream = None
        self._audio_stop = threading.Event()
        self._audio_thread: threading.Thread | None = None
        self._audio_rate = 48000
        self._audio_ch = 1
        self._output_active = False
        self._proc: subprocess.Popen | None = None
        self._srv: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self.compositor = None  # 直播时叠加皮套（live_overlay.OverlayCompositor）
        self.compositor_error = ""

    # ---------------- 预览 ----------------

    def start_preview(self, hwnd: int, audio_device: int) -> None:
        if windows_capture is None:
            raise CaptureError("windows-capture 未安装")
        with self._lock:
            if self.previewing:
                raise CaptureError("预览已在运行")
            self.error = ""
            self._frame = self._jpeg = None
            self._source_frame = None
            self.latest = self._jpeg_at = 0.0
            self._capture_times.clear()
            self._render_times.clear()
            self._process_times.clear()
            self._capture_closed.clear()
        hwnd = int(hwnd)
        _validate_window(hwnd)
        # 必须先启动 WGC 再打开 PyAudio 流：PortAudio WASAPI 流会占用主线程
        # COM 状态，令其后才调用的 start_free_threaded 抛 TypeError（实测）。
        cap = windows_capture.WindowsCapture(
            window_hwnd=hwnd, cursor_capture=False, draw_border=True,
            minimum_update_interval=33)

        def on_frame_arrived(frame, capture_control):
            self._on_frame(frame)

        def on_closed():
            self._on_closed()

        cap.event(on_frame_arrived)
        cap.event(on_closed)
        try:
            control = cap.start_free_threaded()
        except Exception as exc:
            raise CaptureError(_safe(f"窗口采集启动失败: {exc}")) from exc
        p: pa.PyAudio | None = None
        stream = None
        try:
            devs, _d = _list_loopback_devices()
            dev = next((d for d in devs if d["id"] == int(audio_device)), None)
            if dev is None:
                raise CaptureError("音频设备不是有效的 loopback 设备")
            p = pa.PyAudio()
            try:
                info = p.get_device_info_by_index(dev["id"])
            except Exception as exc:
                raise CaptureError(_safe(f"音频设备不可用: {exc}")) from exc
            rate = int(float(info.get("defaultSampleRate") or 48000)) or 48000
            ch = max(1, min(2, int(info.get("maxInputChannels") or 1)))
            # 不用 stream_callback：本机 PyAudioWPatch 0.2.12.9 回调模式会
            # 损坏主线程锁等待（实测确定性 TypeError），改独立阻塞读线程。
            stream = p.open(format=pa.paInt16, channels=ch, rate=rate,
                            input=True, input_device_index=dev["id"],
                            frames_per_buffer=_AUDIO_CHUNK)
        except BaseException as exc:
            try:
                control.stop()
                control.wait()
            except Exception:
                pass
            if stream:
                try:
                    stream.stop_stream(); stream.close()
                except Exception:
                    pass
            if p:
                try:
                    p.terminate()
                except Exception:
                    pass
            if isinstance(exc, CaptureError):
                raise
            raise CaptureError(_safe(f"音频启动失败: {exc}")) from exc
        with self._lock:
            self.hwnd, self.audio_device = hwnd, dev["id"]
            self._audio_rate, self._audio_ch = rate, ch
            self._pa, self._stream, self._control = p, stream, control
            self._crop = _client_crop(hwnd)
            self.previewing = True
            self.started_at = time.time()
            self._stop_evt.clear()
        self._audio_stop.clear()
        self._audio_thread = threading.Thread(target=self._audio_reader,
                                              name="lc-audio", daemon=True)
        self._audio_thread.start()
        self._jpeg_thread = threading.Thread(target=self._jpeg_worker, name="lc-jpeg", daemon=True)
        self._jpeg_thread.start()
        # 等待首帧，过滤纯黑或空白加载画面。
        deadline = time.monotonic() + _FRAME_WAIT_S
        while time.monotonic() < deadline:
            with self._frame_cv:
                if self._frame is None and not self._capture_closed.is_set():
                    self._frame_cv.wait(timeout=0.5)
                got, mean = self._frame, self._mean
            if got is None:
                if self._capture_closed.is_set():
                    break
                continue
            if mean > 6.0:
                return
            if self._capture_closed.is_set():
                break
        self.stop_preview()
        raise CaptureError("采集无有效画面（窗口黑屏或已关闭），已回退")

    def _on_frame(self, frame, *_args) -> None:
        try:
            buf = np.asarray(frame.frame_buffer, dtype=np.uint8)
            if buf.ndim != 3 or buf.shape[2] != 4:
                return
            h, w = buf.shape[:2]
            with self._lock:
                self._raw_size = (w, h)
            if w < 2 or h < 2:
                return
            crop = self._crop
            if crop:
                frame_aspect = w / h
                contains_frame = abs(frame_aspect - crop["window_aspect"]) < abs(frame_aspect - crop["client_aspect"])
                if contains_frame:
                    inset = 2
                    x = int(crop["left"] * w) + inset
                    y = int(crop["top"] * h) + inset
                    cw = max(1, int(crop["width"] * w) - inset * 2)
                    ch = max(1, int(crop["height"] * h) - inset * 2)
                    buf = buf[y:y + ch, x:x + cw]
                    h, w = buf.shape[:2]
            s = max(OUT_W / w, OUT_H / h)
            nw, nh = max(OUT_W, int(round(w * s))), max(OUT_H, int(round(h * s)))
            # INTER_AREA 放大会退化成最近邻（边缘发硬），只有缩小时才拿它抗锯齿
            # WGC 的缓冲区只在回调期间有效。尺寸相同时只复制一次；缩放产生的
            # 新数组已由我们持有，裁切视图也能直接保留，不必再复制整张 1080p。
            scaled = (buf.copy() if (nw, nh) == (w, h) else
                      cv2.resize(buf, (nw, nh),
                                 interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR))
            x0, y0 = (nw - OUT_W) // 2, (nh - OUT_H) // 2
            canvas = scaled[y0:y0 + OUT_H, x0:x0 + OUT_W]
            tiny = cv2.resize(canvas, (16, 9), interpolation=cv2.INTER_AREA)
            rgb = tiny[2:-2, 3:-3, :3]
            mean = float(rgb.mean()) if float(rgb.std()) > 3.0 else 0.0
            with self._lock:
                self._source_frame = canvas
                self._mean = mean
                self.latest = time.monotonic()
                self._capture_times.append(self.latest)
        except Exception as exc:
            self._fail(_safe(f"帧处理异常: {exc}"))

    def _compose_frame(self, source: np.ndarray) -> None:
        canvas = source.copy()
        compositor = self.compositor
        if compositor is not None:
            try:
                compositor.apply(canvas)
            except Exception as exc:
                self.compositor_error = f"Live2D 画面合成失败：{exc}"
                self.compositor = None
        data = canvas.tobytes()
        with self._frame_cv:
            self._frame = data
            self._render_times.append(time.monotonic())
            self._frame_cv.notify_all()

    def _on_closed(self, *_args) -> None:
        self._capture_closed.set()
        with self._frame_cv:
            self._frame_cv.notify_all()

    def _audio_reader(self) -> None:
        try:
            while not self._audio_stop.is_set():
                with self._lock:
                    stream = self._stream
                if stream is None:
                    break
                try:
                    data = stream.read(_AUDIO_CHUNK,
                                       exception_on_overflow=False)
                except (IOError, OSError):
                    if not self._audio_stop.is_set():
                        self._fail("声音输出设备已断开，请停止直播并重新选择设备")
                    break
                self.audio_level = min(1.0, _rms(data))
                if self._output_active:
                    try:
                        self._audio_q.put_nowait(data)
                    except queue.Full:
                        pass  # 丢弃溢出数据，绝不阻塞采集
        except Exception as exc:
            self._fail(_safe(f"音频读取异常: {exc}"))

    def _jpeg_worker(self) -> None:
        # 独立刷新最终画面：游戏静止时皮套仍能动，关掉或移动皮套不会残留旧画面。
        last_frame = None
        step = 1 / FPS
        # 先睡 33ms 再干活会把周期变成「33ms + 编码耗时」，实测掉到 ~16fps。
        # 改成按固定截止时间追帧，编码时间落在同一个周期里，才能跑到采集帧率。
        next_t = time.monotonic()
        while not self._audio_stop.is_set() and self.previewing:
            delay = next_t - time.monotonic()
            if delay > 0 and self._audio_stop.wait(delay):
                break
            next_t += step
            if next_t < time.monotonic() - step:
                next_t = time.monotonic()          # 落后太多就重新对齐，不追补旧帧
            started = time.monotonic()
            source = None
            try:
                with self._lock:
                    source = self._source_frame
                if source is None:
                    continue
                self._compose_frame(source)
                with self._lock:
                    frame = self._frame
                # 必须比较完整画面。16x9 均值指纹会吞掉文字、鼠标和嘴型等局部变化；
                # bytes 比较在 C 层完成，有变化时会提前结束，也保留了静止画面免编码。
                if frame == last_frame:
                    continue
                bgra = np.frombuffer(frame, np.uint8).reshape(OUT_H, OUT_W, 4)
                bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
                ok, encoded = cv2.imencode(".jpg", bgr,
                                           [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if ok:
                    with self._frame_cv:
                        self._jpeg = encoded.tobytes()
                        self._jpeg_at = time.monotonic()
                        self._jpeg_seq += 1
                        self._frame_cv.notify_all()
                    last_frame = frame
            except Exception as exc:
                self._fail(_safe(f"预览编码异常: {exc}"))
                return
            finally:
                if source is not None:
                    with self._lock:
                        self._process_times.append((time.monotonic() - started) * 1000)

    def wait_jpeg(self, after_seq: int, timeout: float = 0.5):
        """等一帧比 after_seq 更新的预览 JPEG，返回 (序号, bytes)；超时或停止返回 None。
        生产 30fps、消费也 30fps 轮询的话平均只能捡到一半，所以预览和源画面走事件等待。"""
        deadline = time.monotonic() + timeout
        with self._frame_cv:
            while True:
                if self._jpeg is not None and self._jpeg_seq > after_seq:
                    return self._jpeg_seq, self._jpeg
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._audio_stop.is_set() or not self.previewing:
                    return None
                self._frame_cv.wait(remaining)

    def frame_jpeg(self) -> bytes | None:
        with self._lock:
            return self._jpeg

    def pixel_scale(self) -> float:
        """显示器缩放倍数（1.0 / 1.25 / 1.5 …）。

        采集帧一直是物理像素（实测等于 DWM 的扩展边框矩形），但进程不能开 DPI 感知
        （一开，WGC 采 Edge/Chromium 窗口就在 ntdll 里访问违例崩掉整个服务），所以窗口
        尺寸读到的是虚拟化后的逻辑值，只能拿两者一除换算。宽度上采集帧只比客户区多
        几个像素的边框，误差 0.2% 左右，够用。
        """
        with self._lock:
            raw, hwnd = self._raw_size, self.hwnd
        if not raw or not hwnd:
            return 1.0
        cw, _ch = client_size(hwnd)
        return raw[0] / cw if cw > 1 else 1.0

    def snapshot(self) -> dict:
        now = time.monotonic()

        def rate(samples):
            recent = [at for at in samples if now - at < 3]
            if len(recent) < 2:
                return 0.0
            # 把距离最后一次更新的空档也算进去，停帧不能还显示旧的 30fps。
            return round((len(recent) - 1) / max(now - recent[0], 0.001), 1)

        with self._lock:
            return {"previewing": self.previewing,
                    "output_running": self.output_running,
                    "audio_level": self.audio_level,
                    "error": self.error, "width": OUT_W, "height": OUT_H,
                    "capture_size": list(self._raw_size or []), "fps": FPS,
                    "capture_fps": rate(self._capture_times),
                    "render_fps": rate(self._render_times),
                    "frame_process_ms": round(sum(self._process_times) / len(self._process_times), 1)
                    if self._process_times else 0.0,
                    "capture_age_ms": round((now - self.latest) * 1000) if self.latest else None}

    # ---------------- 输出 ----------------

    def start_output(self, target: str | Path) -> None:
        if not self.previewing:
            raise CaptureError("请先启动预览，输出只复用现有预览")
        if self.output_running:
            raise CaptureError("输出已在运行")
        if not os.path.isfile(FFMPEG_PATH):
            raise CaptureError("本机 ffmpeg 不可用")
        target = str(target)
        if not target:
            raise CaptureError("输出目标为空")
        with self._lock:
            if self._frame is None:
                raise CaptureError("预览无有效帧")
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))  # 仅本机回环，只接受一条连接
        srv.listen(1)
        port = srv.getsockname()[1]
        srv.settimeout(0.5)
        cmd = [FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
               "-progress", "pipe:2",
               "-thread_queue_size", "8", "-f", "rawvideo",
               "-pixel_format", "bgra", "-video_size", f"{OUT_W}x{OUT_H}",
               "-framerate", str(FPS), "-i", "pipe:0",
               "-thread_queue_size", "64", "-f", "s16le",
               "-ar", str(self._audio_rate), "-ac", str(self._audio_ch),
               "-i", f"tcp://127.0.0.1:{port}",
               "-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll",
               "-b:v", "6000k", "-maxrate", "6000k", "-bufsize", "12000k",
               "-g", "60", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
               "-f", "flv", "-flvflags", "no_duration_filesize", target]
        self._srv = srv
        self.encoded_frames = 0
        self._stop_evt.clear()
        self._enc_evt.clear()
        self._capture_closed.clear()
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            self._close_output_sockets()
            raise CaptureError(_safe(f"ffmpeg 启动失败: {exc}")) from exc
        try:
            _bind_to_app(self._proc.pid)
        except Exception:
            pass
        with self._lock:
            self.output_running = True
        self._output_active = True
        self._threads = [threading.Thread(target=self._pump_video,
                                          name="lc-vid", daemon=True),
                         threading.Thread(target=self._pump_audio,
                                          name="lc-aud", daemon=True),
                         threading.Thread(target=self._read_progress,
                                          name="lc-prog", daemon=True),
                         threading.Thread(target=self._monitor,
                                          name="lc-mon", daemon=True)]
        for t in self._threads:
            t.start()
        # 视频泵先跑 + TCP 监听先建，避免 ffmpeg 顺序打开输入时死锁。
        if not self._enc_evt.wait(timeout=_ENC_READY_S):
            err = self.error
            alive = bool(self._proc and self._proc.poll() is None)
            self.stop_output()
            raise CaptureError(err or ("编码器启动超时" if alive
                                       else "ffmpeg 提前退出"))

    def _pump_video(self) -> None:
        try:
            interval = 1.0 / FPS
            next_t = time.monotonic()
            while not self._stop_evt.is_set() and self._proc:
                with self._lock:
                    data = self._frame
                if data is None:
                    if self._capture_closed.is_set():
                        break
                    time.sleep(0.05)
                    continue
                try:
                    self._proc.stdin.write(data)
                except (OSError, ValueError):
                    break
                next_t += interval
                delay = next_t - time.monotonic()
                if delay > 0:
                    self._stop_evt.wait(delay)
                else:
                    next_t = time.monotonic()
        except Exception as exc:
            self._fail(_safe(f"视频管道异常: {exc}"))
        finally:
            try:
                if self._proc and self._proc.stdin:
                    self._proc.stdin.close()
            except OSError:
                pass

    def _pump_audio(self) -> None:
        conn = None
        try:
            while conn is None and not self._stop_evt.is_set():
                try:
                    conn, _addr = self._srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
            if conn is None:
                return
            self._conn = conn
            conn.settimeout(0.5)
            while not self._stop_evt.is_set() and self._proc:
                try:
                    data = self._audio_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    conn.sendall(data)
                except OSError:
                    break
        except Exception as exc:
            self._fail(_safe(f"音频管道异常: {exc}"))
        finally:
            if conn:
                try:
                    conn.close()
                except OSError:
                    pass

    def _read_progress(self) -> None:
        rc = None
        try:
            for raw in iter(self._proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("frame="):
                    try:
                        n = int(line.split("=", 1)[1])
                    except ValueError:
                        n = 0
                    if n > self.encoded_frames:
                        self.encoded_frames = n
                    if n > 0:
                        self._enc_evt.set()
                elif line and not self._enc_evt.is_set():
                    self._classify(line)
            rc = self._proc.wait(timeout=5)
        except Exception as exc:
            self._fail(_safe(f"进度读取异常: {exc}"))
        if rc not in (None, 0) and not self._stop_evt.is_set():
            self._fail("ffmpeg 输出进程异常退出")

    def _classify(self, line: str) -> None:
        low = line.lower()
        if "nvenc" in low or "cuda" in low:
            self.error = "硬件编码器 h264_nvenc 初始化失败"
        elif "connection" in low or "resolve" in low or "input/output" in low:
            self.error = "推流连接中断"
        else:
            self.error = _safe("ffmpeg 错误: " + line[:120])

    def _monitor(self) -> None:
        try:
            while self.output_running and not self._stop_evt.is_set():
                if self._capture_closed.is_set():
                    self._fail("窗口采集已关闭（最小化或退出），停止输出")
                    break
                try:
                    info = _hwnd_info(self.hwnd, want_title=False)
                except Exception:
                    info = None
                if info is None:
                    self._fail("目标窗口已关闭，停止输出")
                    break
                if info["iconic"] or not info["visible"]:
                    self._fail("目标窗口不可见/已最小化，停止输出")
                    break
                self._crop = _client_crop(self.hwnd) or self._crop
                if self._proc and self._proc.poll() is not None:
                    break
                self._stop_evt.wait(0.5)
        except Exception as exc:
            self._fail(_safe(f"监控线程异常: {exc}"))
        # 仅在非用户主动停止时兜底（窗口失效等）；正常 stop 路径已设事件。
        if self.output_running and not self._stop_evt.is_set():
            self.stop_output()

    def _fail(self, msg: str) -> None:
        with self._lock:
            if not self.error:
                self.error = msg

    def stop_output(self) -> None:
        cur = threading.current_thread()
        self._stop_evt.set()
        self._output_active = False
        self._close_output_sockets()
        proc = self._proc
        # 先解除 ffmpeg 自身（stdin 由视频泵关闭；超时则 terminate），
        # 否则 _read_progress 会阻塞在 readline 上导致 join 超时。
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        for t in self._threads:
            if t is not cur and t.is_alive():
                t.join(timeout=2.0)
        if proc:
            for fd in (proc.stdin, proc.stderr):
                try:
                    if fd:
                        fd.close()
                except OSError:
                    pass
        with self._lock:
            self._proc = None
            self.output_running = False
        # 清空积压音频，避免下次启动带旧数据
        while True:
            try:
                self._audio_q.get_nowait()
            except queue.Empty:
                break

    def _close_output_sockets(self) -> None:
        for s in (self._conn, self._srv):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        with self._lock:
            self._srv = None
            self._conn = None

    def stop_preview(self) -> None:
        self.stop_output()
        with self._lock:
            control, stream, p = self._control, self._stream, self._pa
            self._control = self._stream = self._pa = None
            self.previewing = False
        if control:
            try:
                control.stop()
                control.wait()
            except Exception:
                pass
        self._audio_stop.set()
        with self._lock:
            reader, self._audio_thread = self._audio_thread, None
            encoder, self._jpeg_thread = self._jpeg_thread, None
        if reader and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=2.0)
        if encoder and encoder.is_alive() and encoder is not threading.current_thread():
            encoder.join(timeout=2.0)
        if stream:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if p:
            try:
                p.terminate()
            except Exception:
                pass
        with self._lock:
            self._frame = self._jpeg = None
            self._source_frame = None
            self.latest = self.audio_level = 0.0
            self._crop = None
            self.error = ""
        self._capture_closed.set()

def _rms(data: bytes) -> float:
    if not data:
        return 0.0
    n = len(data) // 2
    if n == 0:
        return 0.0
    samples = np.frombuffer(data[: n * 2], dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples)) / 32768.0)
