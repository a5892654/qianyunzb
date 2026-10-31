import asyncio
import math
import os
import subprocess
import sys
import time

from live_capture import (CaptureError, OUT_H, OUT_W, WindowStreamCapture,
                          close_window, fit_borderless_client, sources, window_pixel_scale)
from live_overlay import Live2DOverlay, OverlayCompositor, OverlayError, find_edge
from live_rice import RiceCompositor
from live_subtitles import SourceSubtitles
from live_theme import StreamTheme, DEFAULTS as THEME_DEFAULTS
from live_platform import (BiliBroadcastPlatform, FaceAuthRequiredError,
                           PlatformError)

# 逻辑视口保持 720；半身取景在 WebGL 中放大，实际像素按上镜尺寸增加。
# 避免先裁成 360px 再放大，也不必传输高分辨率全身画布。
OVERLAY_BOX = 720
# 源画面窗口：给直播姬「窗口采集」用的那个窗口，标题要独特、好认
SOURCE_TITLE = "千音源画面"
SOURCE_W, SOURCE_H = OUT_W, OUT_H
SOURCE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "tools", "source_window.py")
_SOURCE_FIT_S = 6.0      # 等源画面窗口出现的最长时间


def source_python() -> str:
    """使用项目解释器；原生模式只需标准库，旧网页模式还需 pywebview。"""
    venv = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".venv", "Scripts", "python.exe")
    return venv if os.path.isfile(venv) else sys.executable


def song_snapshot(app):
    """点歌状态快照，供直播画面底部的深海电台条显示。"""
    songs = getattr(app, "songs", None)
    if songs is None:
        return None
    song = getattr(songs, "current", None)
    data = None
    if song:
        try:
            pos, dur = app.player.music_progress()
        except Exception:
            pos, dur = 0.0, 0.0
        try:
            levels = app.player.music_levels()
        except Exception:
            levels = None
        data = {"qid": song.get("qid"), "name": song.get("name", ""), "artist": song.get("artist", ""),
                "by": song.get("by", ""), "source": song.get("source", "request"),
                "loading": bool(song.get("loading")), "paused": bool(getattr(songs, "paused", False)),
                "pos": pos, "dur": dur or float(song.get("dur") or 0), "levels": levels}
    return {"enabled": bool(songs.cfg.get("enabled", True)), "song": data}


class Broadcast:
    def __init__(self, app):
        self.app = app
        self.capture = WindowStreamCapture()
        self.source_subtitles = SourceSubtitles(lambda: app.cfg.get("overlay", {}))
        self.capture.source_subtitles = self.source_subtitles
        self.theme = StreamTheme(lambda: app.cfg.get("stream_theme", {}), lambda: song_snapshot(app))
        self.capture.theme_compositor = self.theme
        if getattr(app, "rice", None) is not None:
            self.capture.rice_compositor = RiceCompositor(app.rice, lambda: app.cfg.get("rice_counter", {}))
        self.platform = None
        self.lock = asyncio.Lock()
        self.phase = "idle"
        self.error = ""
        self.started_at = 0.0
        self.watch_task = None
        self.overlay: Live2DOverlay | None = None
        self.overlay_error = ""
        self.source_proc = None
        self.source_hwnd = 0
        self.source_owns_preview = False

    @property
    def source_open(self) -> bool:
        if self.source_proc is None:
            return False
        if self.source_proc.poll() is None:
            return True
        self.capture.stop_source_frames()
        return False

    @property
    def running(self):
        return bool(self.capture.output_running or (self.platform and self.platform.started_room_id))

    def overlay_settings(self) -> tuple[str, float, float, float | None, float | None] | None:
        """每帧调用：开关关掉时返回 None，合成器据此跳过叠加。"""
        l2d = self.app.cfg.get("live2d", {})
        if not l2d.get("enabled") or not l2d.get("overlay_in_broadcast", True):
            return None

        if self.theme.active:
            theme = self.app.cfg.get("stream_theme", {})
            return ("bl", theme.get("avatar_size", THEME_DEFAULTS["avatar_size"]), 0,
                    theme.get("avatar_x", THEME_DEFAULTS["avatar_x"]),
                    theme.get("avatar_y", THEME_DEFAULTS["avatar_y"]))

        def number(key, default, low, high):
            value = float(l2d.get(key, default))
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError("Live2D 位置或大小超出范围，请重新调整")
            return value

        return (str(l2d.get("overlay_corner", "br")), number("overlay_size", 0.5, 0.2, 1.4),
                number("overlay_margin", 0.02, 0, 0.15),
                number("overlay_x", 0.5, 0, 1) if l2d.get("overlay_x") is not None else None,
                number("overlay_y", 0.5, 0, 1) if l2d.get("overlay_y") is not None else None)

    def overlay_problem(self) -> str:
        """空串表示要叠加皮套；"off" 表示不需要；其余为不可用原因。"""
        l2d = self.app.cfg.get("live2d", {})
        if not l2d.get("enabled") or not l2d.get("overlay_in_broadcast", True):
            return "off"
        if not l2d.get("model_file"):
            return "皮套叠加已开启，但还没有选择模型文件"
        if not find_edge():
            return "找不到 Microsoft Edge，直播画面不会有皮套"
        return ""

    def overlay_render_settings(self) -> dict:
        """渲染最终取景，保证放大皮套时有足够像素；每帧读取以支持实时调整。"""
        settings = self.overlay_settings()
        height = round(OUT_H * settings[1]) if settings else OVERLAY_BOX
        # 按 128px 分档，拖动大小时避免每帧重建 WebGL 缓冲区。
        size = max(OVERLAY_BOX, math.ceil(height / 128) * 128)
        zoom = self.app.cfg.get("stream_theme", {}).get("avatar_zoom", THEME_DEFAULTS["avatar_zoom"]) if self.theme.active else 1
        return {"size": size, "zoom": zoom}

    async def prepare_overlay(self) -> None:
        """预览/配置变化时准备渲染器，保留失败原因供界面展示和开播校验。"""
        problem = self.overlay_problem()
        if problem == "off":
            await asyncio.to_thread(self.stop_overlay)
            self.overlay_error = ""
            return
        if problem:
            await asyncio.to_thread(self.stop_overlay)
            self.overlay_error = problem
            self.app.push("log", {"level": "warn", "msg": problem})
            return
        try:
            self.overlay_settings()
        except (TypeError, ValueError) as error:
            self.overlay_error = str(error)
            return
        port = int(self.app.cfg.get("web_port", 8787))
        url = f"http://127.0.0.1:{port}/live2d?overlay=1"
        overlay = self.overlay or Live2DOverlay(self.overlay_render_settings)
        self.overlay = overlay
        try:
            await asyncio.to_thread(overlay.start, url, OVERLAY_BOX)
        except (OverlayError, OSError) as error:
            self.overlay_error = str(error)
            self.app.push("log", {"level": "warn", "msg": f"皮套叠加未启用：{error}"})
            await asyncio.to_thread(self.stop_overlay)
            return
        self.overlay_error = ""
        self.capture.compositor_error = ""
        if getattr(self.capture, "compositor", None) is None:
            self.capture.compositor = OverlayCompositor(overlay, self.overlay_settings)
            self.app.push("log", {"level": "info", "msg": "皮套已叠加到直播画面"})

    async def refresh_overlay(self) -> None:
        async with self.lock:
            if self.capture.previewing:
                await self.prepare_overlay()

    def stop_overlay(self) -> None:
        self.capture.compositor = None
        self.capture.compositor_error = ""
        overlay, self.overlay = self.overlay, None
        if overlay is not None:
            overlay.stop()

    async def account(self):
        try:
            result = await BiliBroadcastPlatform(self.app.cfg.get("bili_cookie", "")).account()
        except PlatformError as error:
            result = {"ok": False, "logged_in": False, "room_id": 0, "error": str(error)}
        self.app.bili.login_uid = result.get("uid") if result.get("logged_in") else None
        self.app.bili.login_name = result.get("name", "") if result.get("logged_in") else ""
        return result

    async def login(self):
        result = await self.account()
        if result.get("logged_in"):
            self.app.bili.login_uid = result["uid"]
            self.app.bili.login_name = result["name"]
            if result.get("room_id"):
                import config
                self.app.cfg["room_id"] = result["room_id"]
                config.save(self.app.cfg)
                await self.app.connect()
        else:
            self.app.bili.login_uid, self.app.bili.login_name = None, ""
            self.app.push("log", {"level": "warn", "msg": result.get("error") or "请先登录主播账号"})
        return result

    async def areas(self):
        try:
            return {"ok": True, "areas": await BiliBroadcastPlatform("").areas()}
        except PlatformError as error:
            return {"ok": False, "error": str(error), "areas": []}

    @staticmethod
    def image_problem(data: bytes):
        from io import BytesIO
        from PIL import Image
        if not data or len(data) > 5 * 1024 * 1024:
            return "封面图片需小于 5MB"
        try:
            image = Image.open(BytesIO(data))
            image.verify()
            image = Image.open(BytesIO(data))
            width, height = image.size
        except Exception:
            return "封面必须是有效的 JPG 或 PNG 图片"
        if image.format not in ("JPEG", "PNG"):
            return "封面只支持 JPG 或 PNG"
        if width < 640 or height < 360:
            return "封面尺寸至少 640×360，建议 1280×720（16:9）"
        if not (1.5 <= width / height <= 2.0):
            return "封面比例请使用 16:9 或 3:2"
        return ""

    async def update_cover(self, data: bytes, filename: str, mime: str):
        if not self.app.cfg.get("bili_cookie"):
            return {"ok": False, "error": "请先扫码登录主播账号，再修改直播间封面"}
        problem = self.image_problem(data)
        if problem:
            return {"ok": False, "error": problem}
        try:
            result = await BiliBroadcastPlatform(self.app.cfg.get("bili_cookie", "")).update_cover(data, filename, mime)
            self.app.push("log", {"level": "info", "msg": "直播间封面已提交 B站审核"})
            return result
        except PlatformError as error:
            return {"ok": False, "error": str(error)}

    async def update_notice(self, notice: str):
        try:
            result = await BiliBroadcastPlatform(self.app.cfg.get("bili_cookie", "")).update_notice(notice)
            self.app.push("log", {"level": "info", "msg": "直播间公告已更新"})
            return result
        except PlatformError as error:
            return {"ok": False, "error": str(error)}

    async def sources(self):
        try:
            return {"ok": True, **await asyncio.to_thread(sources)}
        except (CaptureError, OSError) as error:
            return {"ok": False, "error": str(error), "windows": [], "audio": []}

    @staticmethod
    def selection(data):
        if not isinstance(data, dict):
            raise ValueError("请选择游戏窗口和声音设备")
        hwnd, audio = data.get("hwnd"), data.get("audio_device")
        if type(hwnd) is not int or hwnd <= 0 or type(audio) is not int or audio < 0:
            raise ValueError("请选择有效的游戏窗口和声音输出设备")
        return hwnd, audio

    async def preview(self, data):
        async with self.lock:
            if self.running:
                return {"ok": False, "error": "直播期间不能切换采集源，请先停止直播"}
            try:
                hwnd, audio = self.selection(data)
                if hwnd == self.source_hwnd:
                    # 源画面窗口里放的就是这条采集链的输出，采它等于让自己喂自己，
                    # WGC 在这种回环里会连崩带停整个服务。
                    raise ValueError("「%s」窗口就是千音输出的画面，不能采集它自己" % SOURCE_TITLE)
                if (self.capture.previewing and self.capture.hwnd == hwnd
                        and self.capture.audio_device == audio):
                    # 同一个窗口+设备重复点预览很常见（换皮套设置、界面重进都会点）。
                    # 停掉正在用的 WGC 会话再重开，实测会把整个服务崩掉，没必要冒这个险。
                    await self.prepare_overlay()
                    self.phase, self.error = "preview", ""
                    return {"ok": True, "already": True}
                self.error = ""
                if self.capture.previewing:
                    await asyncio.to_thread(self.capture.stop_preview)
                await asyncio.to_thread(self.capture.start_preview, hwnd, audio)
                await self.prepare_overlay()
                self.phase = "preview"
                return {"ok": True}
            except (CaptureError, ValueError, OSError) as error:
                self.error = str(error)
                self.phase = "error"
                return {"ok": False, "error": self.error}

    async def stop_preview(self):
        async with self.lock:
            if self.running:
                return {"ok": False, "error": "请先停止直播，再关闭预览"}
            await asyncio.to_thread(self.capture.stop_preview)
            await asyncio.to_thread(self.stop_overlay)
            self.overlay_error = ""
            self.phase = "idle"
            self.error = ""
            return {"ok": True}

    async def start_source(self, data) -> dict:
        """开一个「源画面」窗口：里面就是千音要播的完整画面（游戏+皮套+字幕）。

        存在的理由：B站对网页/API 开播通道要人脸认证（60024），而直播姬那条通道不用。
        所以这里不去伪造客户端绕认证，而是把画面做成一个普通窗口，让直播姬自己采集、
        自己开播；千音只负责画面合成、配音和弹幕互动，完全不碰平台开播接口。
        """
        if self.running:
            return {"ok": False, "error": "千音正在直播，不需要源画面窗口"}
        was_previewing = bool(self.capture.previewing)
        res = await self.preview(data)
        if not res.get("ok"):
            return res
        if self.source_open:
            return {"ok": True, "already": True, "title": SOURCE_TITLE}
        port = int(self.app.cfg.get("web_port", 8787))
        url = f"http://127.0.0.1:{port}/source?w={SOURCE_W}&h={SOURCE_H}"
        # 窗口尺寸先按显示器缩放折算成逻辑像素，零头交给 fit_borderless_client 闭环补齐
        scale = await asyncio.to_thread(self.capture.pixel_scale) or 1.0
        cmd = [source_python(), SOURCE_SCRIPT, url, SOURCE_TITLE,
               str(round(SOURCE_W / scale)), str(round(SOURCE_H / scale)), "60", "60"]
        # 上一轮残留的同名窗口先记下：按 pid 认不出我们自己的窗口（子进程会 re-exec，
        # 窗口归属在孙进程上），只能认「新出现的窗口」。
        stale = {w["hwnd"] for w in (await asyncio.to_thread(sources)).get("windows", [])
                 if w.get("title") == SOURCE_TITLE}
        try:
            name = self.capture.start_source_frames()
            cmd.extend(["--shared-memory", name])
            self.source_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            self.capture.stop_source_frames()
            if not was_previewing:
                await self.stop_preview()
            return {"ok": False, "error": f"源画面窗口启动失败：{error}"}
        self.source_owns_preview = not was_previewing
        client = await self.fit_source_window(scale, stale)
        if self.source_proc.poll() is not None or client == (0, 0):
            err = "源画面原生窗口启动失败，请查看本机窗口或共享内存是否可用"
            await self.stop_source()
            self.app.push("log", {"level": "warn", "msg": err})
            return {"ok": False, "error": err}
        self.app.push("log", {"level": "info",
                              "msg": f"源画面窗口已开启：在直播姬里添加「窗口采集」选择「{SOURCE_TITLE}」，"
                                     f"音频选系统声音（游戏声和千音配音都从这里走）"})
        if client != (SOURCE_W, SOURCE_H):
            self.app.push("log", {"level": "warn",
                                  "msg": f"源画面窗口客户区是 {client[0]}x{client[1]}，"
                                         f"和输出 {SOURCE_W}x{SOURCE_H} 不一致，画面会被再缩放一次；"
                                         f"直播姬里请把采集尺寸设为不缩放"})
        return {"ok": True, "title": SOURCE_TITLE, "width": SOURCE_W, "height": SOURCE_H,
                "client": list(client or [0, 0])}

    async def fit_source_window(self, scale: float = 1.0, stale=()) -> tuple[int, int]:
        """等源画面窗口出现，把客户区精确调到输出尺寸，让直播姬采到的每个像素都是原生像素。

        窗口只能按逻辑像素设，所以设的是 SOURCE_W/scale，回报给界面时再乘回缩放。
        `stale` 是开窗口前就在屏幕上的同名窗口（上一轮服务崩掉留下的），那些不归我们管。
        """
        got = (0, 0)
        deadline = time.monotonic() + _SOURCE_FIT_S
        while True:
            for w in (await asyncio.to_thread(sources)).get("windows", []):
                if w.get("title") != SOURCE_TITLE or w["hwnd"] in stale:
                    continue
                # The native source is DPI-aware. Read its exact monitor DPI;
                # estimating from the game's borders can shrink 1920 by a few
                # pixels and introduce another scale operation in live capture.
                scale = await asyncio.to_thread(window_pixel_scale, w["hwnd"], scale)
                target = (max(2, round(SOURCE_W / scale)), max(2, round(SOURCE_H / scale)))
                got = await asyncio.to_thread(fit_borderless_client, w["hwnd"], *target)
                self.source_hwnd = w["hwnd"]
                return (round(got[0] * scale), round(got[1] * scale))
            if time.monotonic() >= deadline:
                return got
            await asyncio.sleep(0.25)

    async def stop_source(self) -> dict:
        self.capture.stop_source_frames()
        proc, self.source_proc = self.source_proc, None
        hwnd, self.source_hwnd = self.source_hwnd, 0
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                await asyncio.to_thread(proc.wait, 5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if hwnd:
            # 那个进程会 re-exec，terminate 只杀得到外壳，窗口得自己关一次
            await asyncio.to_thread(close_window, hwnd)
        owns, self.source_owns_preview = self.source_owns_preview, False
        return await self.stop_preview() if owns else {"ok": True}

    async def start(self, data):
        async with self.lock:
            if self.running:
                return {"ok": False, "error": "千音已经在直播，请勿重复开播"}
            if self.source_open:
                return {"ok": False, "error": "源画面窗口还开着（直播姬在用），请先点「关闭源画面」再用千音开播"}
            try:
                hwnd, audio = self.selection(data)
                if data.get("confirm") is not True:
                    raise ValueError("开播前需要确认公开直播")
                title, area = data.get("title"), data.get("area_id")
                if not isinstance(title, str) or not 1 <= len(title.strip()) <= 60:
                    raise ValueError("直播标题必须为1–60个字符")
                if type(area) is not int or area <= 0:
                    raise ValueError("请选择直播分区")
                if not self.capture.previewing or self.capture.hwnd != hwnd or self.capture.audio_device != audio:
                    raise ValueError("请先预览当前窗口和声音，再开始直播")
                if self.capture.error:
                    raise CaptureError(self.capture.error)
                devices = await asyncio.to_thread(sources)
                if not devices["ffmpeg_ready"]:
                    raise CaptureError("找不到直播编码器，暂不能开播")
                await self.prepare_overlay()
                if self.overlay_error:
                    raise OverlayError(self.overlay_error)
                self.phase, self.error = "starting", ""
                self.platform = BiliBroadcastPlatform(self.app.cfg.get("bili_cookie", ""))
                result = await self.platform.begin(title.strip(), area)
                await asyncio.to_thread(self.capture.start_output, result["url"])
                self.started_at = time.monotonic()
                self.phase = "live"
                self.watch_task = asyncio.create_task(self._watch())
                self.app.push("log", {"level": "info", "msg": f"B站直播已开始，直播间 {result['room_id']}"})
                return {"ok": True, "room_id": result["room_id"]}
            except (PlatformError, CaptureError, OverlayError, ValueError, OSError) as error:
                self.error = str(error)
                # 开播失败只弹在界面上，事后查不到；写进日志才能看清 B站到底回了什么
                self.app.push("log", {"level": "warn", "msg": f"开播失败：{self.error}"})
                await asyncio.to_thread(self.capture.stop_output)
                if self.platform and self.platform.started_room_id:
                    try:
                        await self.platform.end()
                    except PlatformError as stop_error:
                        self.error += "；" + str(stop_error)
                self.phase = "error"
                return {"ok": False, "error": self.error}

    async def _watch(self):
        try:
            while self.phase == "live":
                await asyncio.sleep(1)
                if not self.capture.output_running or self.capture.error:
                    reason = self.capture.error or "视频编码进程已退出"
                    await self.stop()
                    self.phase, self.error = "error", reason
                    return
        except asyncio.CancelledError:
            pass

    async def stop(self):
        async with self.lock:
            self.phase = "stopping"
            if self.watch_task and self.watch_task is not asyncio.current_task():
                self.watch_task.cancel()
                try:
                    await self.watch_task
                except asyncio.CancelledError:
                    pass
            self.watch_task = None
            await asyncio.to_thread(self.capture.stop_output)
            try:
                if self.platform:
                    await self.platform.end()
            except PlatformError as error:
                self.phase, self.error = "error", "本地推流已停止，但 B站停播未完成：" + str(error)
                return {"ok": False, "error": self.error}
            self.phase = "preview" if self.capture.previewing else "idle"
            if not self.capture.previewing:
                await asyncio.to_thread(self.stop_overlay)
            self.error = ""
            return {"ok": True}

    async def stop_external(self, room_id):
        """只在明确选择直播姬停播时使用，不改变普通关闭/退出的清理范围。"""
        async with self.lock:
            try:
                return await BiliBroadcastPlatform(self.app.cfg.get("bili_cookie", "")).end_external(room_id)
            except PlatformError as error:
                return {"ok": False, "error": str(error)}

    async def close(self):
        result = await self.stop()
        await self.stop_source()
        await asyncio.to_thread(self.capture.stop_preview)
        await asyncio.to_thread(self.stop_overlay)
        return result

    def status(self):
        state = self.capture.snapshot()
        if state.get("error") and not self.running:
            self.phase = "error"
        overlay = self.overlay
        enabled = bool(self.app.cfg.get("live2d", {}).get("enabled") and
                       self.app.cfg.get("live2d", {}).get("overlay_in_broadcast", True))
        overlay_error = (self.overlay_error or (overlay.error if overlay else "") or
                         getattr(self.capture, "compositor_error", "")) if enabled else ""
        ready = bool(enabled and not overlay_error and getattr(self.capture, "compositor", None) and
                     overlay and overlay.frame() is not None)
        return {**state, "phase": self.phase, "error": self.error or state.get("error", "") or state.get("theme_error", ""),
                "running": self.running, "overlay": ready, "source": self.source_open,
                "source_title": SOURCE_TITLE if self.source_open else "",
                "overlay_error": overlay_error,
                "seconds": time.monotonic() - self.started_at if self.running and self.started_at else 0}

    def frame(self):
        return self.capture.frame_jpeg()
