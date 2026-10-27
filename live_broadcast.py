import asyncio
import math
import time

from live_capture import CaptureError, WindowStreamCapture, sources
from live_overlay import Live2DOverlay, OverlayCompositor, OverlayError, find_edge
from live_platform import BiliBroadcastPlatform, PlatformError

OVERLAY_BOX = 720


class Broadcast:
    def __init__(self, app):
        self.app = app
        self.capture = WindowStreamCapture()
        self.platform = None
        self.lock = asyncio.Lock()
        self.phase = "idle"
        self.error = ""
        self.started_at = 0.0
        self.watch_task = None
        self.overlay: Live2DOverlay | None = None
        self.overlay_error = ""

    @property
    def running(self):
        return bool(self.capture.output_running or (self.platform and self.platform.started_room_id))

    def overlay_settings(self) -> tuple[str, float, float, float | None, float | None] | None:
        """每帧调用：开关关掉时返回 None，合成器据此跳过叠加。"""
        l2d = self.app.cfg.get("live2d", {})
        if not l2d.get("enabled") or not l2d.get("overlay_in_broadcast", True):
            return None

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
        overlay = self.overlay or Live2DOverlay()
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

    async def start(self, data):
        async with self.lock:
            if self.running:
                return {"ok": False, "error": "千音已经在直播，请勿重复开播"}
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

    async def close(self):
        result = await self.stop()
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
        return {**state, "phase": self.phase, "error": self.error or state.get("error", ""),
                "running": self.running, "overlay": ready,
                "overlay_error": overlay_error,
                "seconds": time.monotonic() - self.started_at if self.running and self.started_at else 0}

    def frame(self):
        return self.capture.frame_jpeg()
