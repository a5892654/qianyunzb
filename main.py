import asyncio
import collections
import ctypes
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import webbrowser

from aiohttp import web, WSMsgType, ClientSession

import config
import live2d
from bili import BiliLive, QrLogin, SmsLogin
from brain import Brain, default_qoder_exe, list_qoder_models
from dtgame.bridge import BridgeError, GameBridge
from dtgame.launcher import GameProcess, find_godot
from dtgame.player import Player as DTPlayer
from dtgame.rice import RiceTracker
from live_rice import RiceCounter
from live_broadcast import Broadcast
from live_schedule import DailyBroadcastSchedule
from pipeline import Pipeline
from songs import SongManager
from netease import CAPTCHA_ID, DEVICE_APP_ID
from tts import GsvEngine, Player
from wishlist import WishlistActions, validate_settings as validate_wishlist_settings

os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.FileHandler(os.path.join(config.LOG_DIR, "app.log"), encoding="utf-8"),
              logging.StreamHandler(sys.stdout)])
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("app")

# aiohttp 默认只收 1MB 请求体，封面原图轻松超它；上限要比 5MB 的封面校验再宽一点。
MAX_BODY_BYTES = 8 * 1024 * 1024

_PHONE_RE = re.compile(r"\d{6,11}")


def _sms_fail(e: Exception) -> str:
    """B站短信接口的报错原样给用户看，但兜底把手机号抹掉，别进日志。"""
    return _PHONE_RE.sub("<手机号>", str(e) or type(e).__name__)[:120]


class UiLogHandler(logging.Handler):
    def __init__(self, app):
        super().__init__(logging.WARNING)
        self.app = app

    def emit(self, record):
        if record.name in ("ai", "tts", "bili", "app"):
            self.app.push("log", {"level": "warn" if record.levelno < logging.ERROR else "error",
                                  "msg": record.getMessage()})


class App:
    def __init__(self):
        self.cfg = config.load()
        self.schedule = DailyBroadcastSchedule(self)
        self.loop = None
        self.ws = set()
        self.history = collections.deque(maxlen=300)
        self.brain = Brain()
        self.engine = GsvEngine()
        self.player = Player()
        self.bili = BiliLive(self.on_event, lambda m: self.push("log", {"level": "info", "msg": m}))
        self.pipe = Pipeline(self)
        self.songs = SongManager(self)
        self.wishlist = WishlistActions(self)
        self.qr = QrLogin()
        self.sms = SmsLogin()
        self.dt = None
        self.dt_task = None
        self.dt_resume_lock = asyncio.Lock()
        self.engine_ready = asyncio.Event()
        self.game = GameProcess(self.cfg.get("dtgame", {}))
        self.rice = RiceCounter()
        self.broadcast = Broadcast(self)
        self.status = {}
        logging.getLogger().addHandler(UiLogHandler(self))

    # ---------- 推送到界面 ----------
    def push(self, kind, data):
        msg = {"kind": kind, "t": time.strftime("%H:%M:%S"), **data}
        if kind in ("event", "reply", "log"):
            self.history.append(msg)
        if kind == "log":
            print(f"[{msg['t']}] {data.get('msg')}", flush=True)
        elif kind == "reply":
            log.info("回复[%s] %s  <- %s", data.get("src"), data.get("text"), data.get("desc"))
        text = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.ws):
            if not ws.closed:
                asyncio.ensure_future(ws.send_str(text))

    def on_event(self, ev):
        self.pipe.on_event(ev)

    # ---------- 后台任务 ----------
    async def status_loop(self):
        while True:
            tts = self.cfg["tts"]
            if not tts.get("enabled", True):
                eng = "已关闭"
            elif tts.get("stream_enabled") and tts.get("qwen_enabled"):
                from dtgame import ttsd
                eng = "在线" if await ttsd.alive(tts) else "启动中" if not self.engine_ready.is_set() else "未启动"
            elif self.engine.lock.locked():
                eng = "合成中"
            else:
                a = await self.engine.alive(tts["api_url"])
                eng = {True: "在线", None: "忙碌", False: "未启动"}[a]
            ai = self.cfg["ai"]
            self.status = {
                "bili": ("已连接 " + str(self.bili.room_id)) if self.bili.connected else ("连接中…" if self.bili.client else "未连接"),
                "login": self.bili.login_name or "未登录",
                "ai": f"Qoder · {ai.get('qoder_model') or '默认'}" if ai.get("backend") != "openai" else f"API · {ai.get('openai_model')}",
                "engine": eng,
                "queue": len(self.pipe.jobs),
                "paused": self.pipe.paused,
                "speaking": bool(self.pipe.current),
            }
            self.push("status", {"status": self.status})
            await asyncio.sleep(2)

    async def startup(self):
        self.pipe.start()
        self.schedule.start()
        self.tasks = [asyncio.create_task(self.status_loop()), asyncio.create_task(self.songs.run()),
                      asyncio.create_task(self.brain.warm(self.cfg))]
        if self.cfg["tts"].get("enabled") and self.cfg["tts"].get("auto_start"):
            self.tasks.append(asyncio.create_task(self._boot_engine()))
        else:
            self.engine_ready.set()
        if self.cfg.get("auto_connect") and self.cfg.get("room_id"):
            asyncio.create_task(self.connect())
        if self.cfg.get("dtgame", {}).get("enabled") and "--manual-dt" not in sys.argv:
            r = self.start_dt()
            if not r.get("ok"):
                self.push("log", {"level": "error", "msg": f"自动打牌启动失败：{r.get('error')}"})

    async def _boot_engine(self):
        """先把语音引擎拉起来再去代打，否则开场几句会掉到系统语音。"""
        try:
            await self.engine.start(self.cfg["tts"],
                                    lambda m: self.push("log", {"level": "info", "msg": m}))
        except Exception as e:
            self.push("log", {"level": "error", "msg": f"语音引擎启动失败：{e}"})
        finally:
            self.engine_ready.set()

    async def connect(self):
        self.push("log", {"level": "info", "msg": f"正在连接直播间 {self.cfg['room_id']} …"})
        try:
            await self.bili.start(self.cfg["room_id"], self.cfg.get("bili_cookie", ""))
        except Exception as e:
            self.push("log", {"level": "error", "msg": f"连接直播间失败：{e}"})

    # ---------- 游戏自动托管 ----------
    def _dt_bridge(self) -> GameBridge:
        c = self.cfg.get("dtgame", {})
        return GameBridge(c.get("bridge_host", "127.0.0.1"), int(c.get("bridge_port", 9787)),
                          grans_port=int(c.get("grans_bridge_port", 9788)))

    def _dt_ai(self) -> dict:
        c = self.cfg.get("dtgame", {})
        ai = dict(self.cfg.get("ai", {}))
        for key in ("openai_base", "openai_key", "openai_model"):
            if c.get(key):
                ai[key] = c[key]
        return ai

    def _config_problem(self) -> str:
        """模型侧的配置检查，返回空串表示没问题。"""
        ai = self._dt_ai()
        if not str(ai.get("openai_base", "")).strip():
            return "还没填接口地址（AI 设置 → OpenAI 兼容 API）"
        if not str(ai.get("openai_model", "")).strip():
            return "还没填模型名"
        if not str(ai.get("openai_key", "")).strip():
            return "还没填 API Key（AI 设置 → OpenAI 兼容 API）"
        return ""

    def dt_ready(self) -> tuple[bool, str]:
        """代打前的体检：模型配好没有、游戏在不在。"""
        why = self._config_problem()
        if why:
            return False, why
        try:
            self._dt_bridge().screen()
        except BridgeError as e:
            return False, str(e)
        return True, ""

    def _remember_chars(self, names) -> None:
        """换角色要跨重启记住：进程一开就忘了上一局是谁，又会连着点同一个人。"""
        if list(self.cfg["dtgame"].get("recent_chars") or []) == list(names):
            return
        self.cfg["dtgame"]["recent_chars"] = list(names)
        config.save(self.cfg)

    def start_dt(self) -> dict:
        """只建 Player 并丢进事件循环，游戏启动/体检放到 async 那一侧做。"""
        if self.dt and self.dt.running:
            return {"ok": False, "error": "自动打牌已经在跑"}
        # 配置问题现在就报，别假装启动成功然后空转。
        why = self._config_problem()
        if why:
            return {"ok": False, "error": why}
        c = self.cfg.get("dtgame", {})
        p = DTPlayer(
            self._dt_ai(),
            self._dt_bridge(),
            speak=self.pipe.narrate,
            log_push=lambda level, msg: self.push("log", {"level": level, "msg": msg}),
            narrate=bool(c.get("narrate", True)),
            pipe=self.pipe,
            emotions=lambda: self.cfg["live2d"].get("emotions", []) if self.cfg["live2d"].get("enabled") else [],
            chars=c.get("recent_chars") or (),
            on_chars=lambda names: self._remember_chars(names),
            auto_depth=bool(c.get("auto_depth", True)),
            depth=c.get("depth", 0),
            observer=RiceTracker(self.rice, lambda: bool(self.cfg.get("rice_counter", {}).get("enabled")), self.rice_awarded),
        )
        p.running = True
        self.dt = p
        self.dt_task = asyncio.create_task(self._dt_supervise(p))
        self.dt_task.add_done_callback(self._task_died)
        return {"ok": True}

    def rice_state(self):
        return {**self.rice.state(self.cfg.get("rice_counter", {}).get("enabled", False)),
                "error": getattr(self.broadcast.capture, "rice_error", "")}

    def rice_awarded(self):
        self.push("rice", self.rice_state())
        self.push("log", {"level": "info", "msg": f"AI 击败第三幕 Boss，白饭 +1，累计 {self.rice.total} 碗！"})

    @staticmethod
    def _task_died(task: asyncio.Task):
        if task.cancelled():
            return
        e = task.exception()
        if e is not None:
            log.error("代打任务异常退出：%r", e, exc_info=e)

    async def _dt_supervise(self, p: DTPlayer):
        """等语音引擎和游戏都就绪再开跑，避免开场几句用错音色。"""
        c = self.cfg.get("dtgame", {})
        if getattr(self, "engine_ready", None) is not None and not self.engine_ready.is_set():
            self.push("log", {"level": "info", "msg": "等语音引擎就绪…"})
            try:
                await asyncio.wait_for(self.engine_ready.wait(), 180)
            except asyncio.TimeoutError:
                self.push("log", {"level": "warn", "msg": "语音引擎等待超时，先开打"})
        self.push("log", {"level": "info", "msg": "正在准备游戏…"})

        ok, why = await asyncio.to_thread(self.dt_ready)
        if not ok and c.get("auto_launch", True):
            launcher = self.game
            res = await launcher.launch_and_wait(self._bridge_alive)
            if not res.get("ok"):
                self.push("log", {"level": "error", "msg": f"游戏启动失败：{res.get('error')}"})
                self.dt = None
                return
            self.push("log", {"level": "info", "msg": "游戏已就绪"})
            ok, why = await asyncio.to_thread(self.dt_ready)
        if not ok:
            self.push("log", {"level": "error", "msg": f"无法开始代打：{why}"})
            self.dt = None
            return

        a = self.cfg.get("audio", {})
        pid = self.game.proc.pid if getattr(self.game, "proc", None) else None
        if a.get("duck_game", True) and pid:
            level = max(0.0, min(1.0, float(a.get("duck_level", 25)) / 100.0))
            self.player.set_duck([pid], level)
            self.push("log", {"level": "info",
                              "msg": "说话时会把游戏声压到 %d%%（语音设置里可关）" % int(level * 100)})

        self.push("log", {"level": "info", "msg": "自动打牌已启动"})
        try:
            await p.run_forever(float(c.get("step_interval", 0.4)))
        finally:
            if self.dt is p:
                self.dt = None

    def _bridge_alive(self) -> bool:
        try:
            self._dt_bridge().call("ping", timeout=1.5)
            return True
        except BridgeError:
            return False

    async def stop_dt(self) -> dict:
        player, task = self.dt, self.dt_task
        if player:
            player.stop()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.dt is player:
            self.dt = None
        if self.dt_task is task:
            self.dt_task = None
        return {"ok": True}

    async def resume_dt(self, new_run: bool = False) -> dict:
        async with self.dt_resume_lock:
            if self.dt and self.dt.running and await asyncio.to_thread(self._bridge_alive):
                return {"ok": True, "already": True}
            await self.stop_dt()
            why = self._config_problem()
            if why:
                return {"ok": False, "error": why}
            if not await asyncio.to_thread(self._bridge_alive):
                result = await self.game.launch_and_wait(self._bridge_alive)
                if not result.get("ok"):
                    return result
            bridge = self._dt_bridge()
            try:
                screen = (await asyncio.to_thread(bridge.screen)).get("screen")
                if screen == "menu" and not new_run:
                    deadline = time.monotonic() + 12
                    while True:
                        result = await asyncio.to_thread(bridge.continue_run)
                        if result.get("ok") or "不在主菜单" not in str(result.get("error") or ""):
                            break
                        if time.monotonic() >= deadline:
                            break
                        await asyncio.sleep(0.5)
                    if not result.get("ok") and not result.get("no_save"):
                        # 账号验证这类卡住必须人看一眼，自动开局等于把弹窗绕过去了
                        return {"ok": False, "code": "resume_pending",
                                "error": result.get("error") or "继续游戏未完成，请查看游戏窗口提示"}
                    if result.get("ok"):
                        screen = (await asyncio.to_thread(bridge.screen)).get("screen")
                    else:
                        self.push("log", {"level": "info", "msg": "没有可继续的存档，自动开始新出征"})
                elif screen == "hall" and not new_run:
                    # 战斗中没有落盘点，重启后回到大厅是常态：直接开新局，别停在原地等人点
                    self.push("log", {"level": "info", "msg": "上次停在出征大厅，自动开始新出征"})
                return self.start_dt()
            except BridgeError as error:
                return {"ok": False, "error": str(error)}

    async def ensure_game_and_dt(self) -> dict:
        """定时开播前把游戏拉起来、代打开跑。

        游戏起不来是真开不了播（没画面可采）；代打起不来只警告，直播照常——
        模型没配好不该让观众看不到画面。
        """
        launched = False
        if not await asyncio.to_thread(self._bridge_alive):
            if not self.cfg.get("dtgame", {}).get("auto_launch", True):
                return {"ok": False, "error": "定时开播未执行：游戏没在跑，且代打的自动启动是关的"}
            res = await self.game.launch_and_wait(self._bridge_alive)
            if not res.get("ok"):
                return {"ok": False, "error": "定时开播未执行：游戏启动失败（%s）" % (res.get("error") or "")}
            launched = True
        r = await self.resume_dt()
        if not r.get("ok") and not r.get("already"):
            self.push("log", {"level": "warn", "msg": "代打没有自动开起来，先照常开播：" + str(r.get("error"))})
        return {"ok": True, "launched": launched}

    async def shutdown(self):
        await self.schedule.close()
        await self.wishlist.close()
        # 先让直播画面立刻隐藏，再收拾后台
        log.info("通知 %d 个显示页面退出", len(self.ws))
        for ws in list(self.ws):
            if not ws.closed:
                try:
                    await asyncio.wait_for(ws.send_str(json.dumps({"kind": "bye"})), 1)
                except (Exception, asyncio.CancelledError):
                    pass
        await asyncio.sleep(0.15)
        for ws in list(self.ws):
            try:
                await asyncio.wait_for(ws.close(), 1)
            except (Exception, asyncio.CancelledError):
                pass
        self.ws.clear()
        await self.broadcast.close()
        await self.stop_dt()
        for t in getattr(self, "tasks", []):
            t.cancel()
        for t in getattr(self, "tasks", []) + getattr(self.pipe, "tasks", []):
            try:
                await asyncio.wait_for(t, 0.5)
            except (Exception, asyncio.CancelledError):
                pass
        for coro in (self.bili.stop(), self.brain.close()):
            try:
                await asyncio.wait_for(coro, 3)
            except (Exception, asyncio.CancelledError):
                pass
        self.player.close()
        if self.game.running():
            self.game.stop()
            log.info("已关闭游戏进程")
        if self.cfg["tts"].get("stop_on_exit", True):
            self.engine.stop(keep_comfyui=bool(self.cfg["tts"].get("comfy_keep_on_exit", True)))


# ---------- HTTP ----------
def pick_path(kind, initial=""):
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if kind == "dir":
            return filedialog.askdirectory(initialdir=initial or None, title="选择文件夹")
        types = [("音频文件", "*.wav *.mp3 *.flac *.ogg *.m4a"), ("所有文件", "*.*")] if kind == "audio" \
            else [("程序", "*.exe"), ("所有文件", "*.*")]
        return filedialog.askopenfilename(initialdir=initial or None, filetypes=types, title="选择文件")
    finally:
        root.destroy()


def build_web(app: App):
    routes = web.RouteTableDef()

    @web.middleware
    async def guard(request, handler):
        host = request.host.split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return web.Response(status=403, text="forbidden")
        if request.method == "POST" and request.headers.get("X-BVB") != "1":
            return web.Response(status=403, text="missing header")
        return await handler(request)

    def ok(**kw):
        return web.json_response({"ok": True, **kw})

    def page(name):
        return web.FileResponse(os.path.join(config.WEB_DIR, name), headers={"Cache-Control": "no-cache"})

    @routes.get("/")
    async def index(_):
        return page("index.html")

    @routes.get("/overlay")
    async def overlay(_):
        return page("overlay.html")

    @routes.get("/rice")
    async def rice_page(_):
        return page("rice.html")

    @routes.get("/api/rice/state")
    async def rice_state(_):
        return web.json_response(app.rice_state())

    @routes.post("/api/rice/preview")
    async def rice_preview(_):
        app.rice.preview()
        state = app.rice_state()
        app.push("rice", state)
        return ok(**state)

    @routes.get("/source")
    async def source_page(_):
        return page("source.html")

    @routes.get("/live2d")
    async def live2d_page(_):
        return page("live2d.html")

    @routes.get("/l2d/model/{path:.*}")
    async def l2d_file(request):
        l2d = app.cfg["live2d"]
        rel = request.match_info["path"]
        if rel == "auto.model3.json":
            try:
                return web.json_response(live2d.build_model_json(l2d))
            except Exception as e:
                return web.json_response({"error": str(e)}, status=404)
        full = live2d.safe_path(l2d["model_dir"], rel)
        if not full:
            return web.Response(status=404)
        return web.FileResponse(full)

    @routes.get("/api/l2d/info")
    async def l2d_info(_):
        l2d = app.cfg["live2d"]
        try:
            exprs, motions = live2d.list_assets(l2d["model_dir"])
            err = "" if os.path.exists(os.path.join(l2d["model_dir"], l2d["model_file"])) else "找不到模型文件"
        except Exception as e:
            exprs, motions, err = [], [], str(e)
        return web.json_response({"cfg": l2d, "expressions": exprs, "motions": motions, "error": err,
                                  "model_files": live2d.list_model_files(l2d["model_dir"])})

    @routes.get("/api/l2d/library")
    async def l2d_library(_):
        l2d = app.cfg["live2d"]
        cur = os.path.basename(os.path.normpath(l2d.get("model_dir") or ""))
        return web.json_response({"root": live2d.LIBRARY_DIR, "models": live2d.list_library(), "current": cur})

    @routes.post("/api/l2d/switch")
    async def l2d_switch(request):
        d = await request.json()
        key = str(d.get("key") or "")
        target = next((x for x in live2d.list_library() if x["key"] == key), None)
        if not target:
            return web.json_response({"ok": False, "error": "模型库中找不到：" + key}, status=404)
        l2d = app.cfg["live2d"]
        profiles = l2d.setdefault("profiles", {})
        cur = os.path.basename(os.path.normpath(l2d.get("model_dir") or ""))
        kept = ("model_file", "idle_motion", "emotions", "scale", "x", "y")
        if cur:
            profiles[cur] = {k: l2d.get(k) for k in kept}
        # Expression and motion ids are model-specific, so each skin remembers its own set.
        prof = profiles.get(key) or {}
        l2d["model_dir"] = target["dir"]
        l2d["model_file"] = target["model_file"]
        l2d["idle_motion"] = prof.get("idle_motion", "")
        l2d["emotions"] = prof.get("emotions", [])
        for k in ("scale", "x", "y"):
            if k in prof:
                l2d[k] = prof[k]
        config.save(app.cfg)
        app.push("l2d_config", {})
        return ok(key=key)

    @routes.post("/api/l2d/test")
    async def l2d_test(request):
        d = await request.json()
        app.push("l2d_test", {"expressions": d.get("expressions", []), "motion": d.get("motion", ""),
                              "emotion": d.get("emotion", ""), "text": d.get("text", "")})
        return ok()

    @routes.get("/ws")
    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        app.ws.add(ws)
        await ws.send_str(json.dumps({"kind": "hello", "history": list(app.history), "status": app.status}, ensure_ascii=False))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            app.ws.discard(ws)
        return ws

    @routes.get("/api/config")
    async def get_config(_):
        c = json.loads(json.dumps(app.cfg))
        c["_qoder_default"] = default_qoder_exe()
        c["_logged_in"] = bool(app.cfg.get("bili_cookie"))
        c.pop("bili_cookie", None)
        c["songs"]["_ne_logged_in"] = bool(c["songs"].pop("netease_cookie", ""))
        if isinstance(c.get("wishlist"), dict):
            c["wishlist"].pop("_completed", None)
        return web.json_response(c)

    @routes.post("/api/config")
    async def set_config(request):
        patch = await request.json()
        patch.pop("bili_cookie", None)
        if isinstance(patch.get("broadcast"), dict) and "schedule" in patch["broadcast"]:
            return web.json_response({"ok": False, "error": "请通过定时直播计划的保存按钮确认修改"}, status=400)
        if isinstance(patch.get("broadcast"), dict) and "auto_stop_minutes" in patch["broadcast"]:
            minutes = patch["broadcast"]["auto_stop_minutes"]
            if type(minutes) is not int or not 0 <= minutes <= 10080:
                return web.json_response({"ok": False, "error": "自动停播时长需为 0 至 10080 分钟的整数"}, status=400)
        if isinstance(patch.get("songs"), dict):
            patch["songs"].pop("netease_cookie", None)
            patch["songs"].pop("_ne_logged_in", None)
        if "rice_counter" in patch:
            rice_patch = patch["rice_counter"]
            if not isinstance(rice_patch, dict) or set(rice_patch) - {"enabled"} or ("enabled" in rice_patch and type(rice_patch["enabled"]) is not bool):
                return web.json_response({"ok": False, "error": "白饭计数开关无效"}, status=400)
        if "wishlist" in patch:
            try:
                if not isinstance(patch["wishlist"], dict):
                    raise ValueError("心愿单动作设置无效")
                patch["wishlist"].pop("_completed", None)
                validate_wishlist_settings({**config.DEFAULTS["wishlist"], **app.cfg.get("wishlist", {}), **patch["wishlist"]})
            except ValueError as error:
                return web.json_response({"ok": False, "error": str(error)}, status=400)
        old_room = app.cfg.get("room_id")
        app.cfg = config.merge(app.cfg, patch)
        config.save(app.cfg)
        if isinstance(patch.get("broadcast"), dict) and "auto_stop_minutes" in patch["broadcast"]:
            app.schedule.refresh_auto_timer()
        if "live2d" in patch:
            app.push("l2d_config", {})
            await app.broadcast.refresh_overlay()
        if "rice_counter" in patch:
            if not app.cfg["rice_counter"].get("enabled") and app.dt:
                app.dt._observe("pause")
            app.push("rice", app.rice_state())
        if "songs" in patch:
            app.songs._apply_volume()
        if "wishlist" in patch:
            app.wishlist.push()
        if str(app.cfg.get("room_id")) != str(old_room) and app.bili.client:
            asyncio.create_task(app.connect())
        return ok()

    @routes.post("/api/bili/connect")
    async def bili_connect(_):
        asyncio.create_task(app.connect())
        return ok()

    @routes.post("/api/bili/disconnect")
    async def bili_disconnect(_):
        await app.bili.stop()
        app.push("log", {"level": "info", "msg": "已断开直播间"})
        return ok()

    @routes.post("/api/bili/qr")
    async def bili_qr(_):
        import qrcode
        import qrcode.image.svg
        url, key = await app.qr.generate()
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
        return ok(svg=img.to_string(encoding="unicode"), key=key)

    @routes.post("/api/bili/qrpoll")
    async def bili_qrpoll(request):
        if app.broadcast.running or app.broadcast.phase in ("starting", "stopping"):
            return web.json_response({"ok": False, "error": "请先停止直播，再切换主播账号"})
        key = (await request.json())["key"]
        code, msg, cookie = await app.qr.poll(key)
        if code == 0 and cookie:
            app.schedule.disable_for_account_change()
            app.cfg["bili_cookie"] = cookie
            config.save(app.cfg)
            asyncio.create_task(app.broadcast.login())
        return ok(code=code, msg=msg)

    @routes.post("/api/bili/sms/captcha")
    async def bili_sms_captcha(_):
        """拿极验参数给前端滑人机验证；滑完才能发短信。"""
        try:
            return ok(**await app.sms.captcha())
        except Exception as e:
            return web.json_response({"ok": False, "error": _sms_fail(e)}, status=400)

    @routes.post("/api/bili/sms/send")
    async def bili_sms_send(request):
        if app.broadcast.running or app.broadcast.phase in ("starting", "stopping"):
            return web.json_response({"ok": False, "error": "请先停止直播，再切换主播账号"})
        d = await request.json()
        phone = str(d.get("phone") or "").strip()
        if not (phone.isdigit() and 6 <= len(phone) <= 11):
            return web.json_response({"ok": False, "error": "手机号格式不对"}, status=400)
        try:
            gee = {"validate": d.get("validate"), "seccode": d.get("seccode")}
            key = await app.sms.send(phone, gee, str(d.get("token") or ""),
                                     str(d.get("challenge") or ""))
        except Exception as e:
            return web.json_response({"ok": False, "error": _sms_fail(e)}, status=400)
        return ok(captcha_key=key)

    @routes.post("/api/bili/sms/login")
    async def bili_sms_login(request):
        if app.broadcast.running or app.broadcast.phase in ("starting", "stopping"):
            return web.json_response({"ok": False, "error": "请先停止直播，再切换主播账号"})
        d = await request.json()
        phone, code = str(d.get("phone") or "").strip(), str(d.get("code") or "").strip()
        if not (phone.isdigit() and 6 <= len(phone) <= 11):
            return web.json_response({"ok": False, "error": "手机号格式不对"}, status=400)
        if not (code.isdigit() and 4 <= len(code) <= 8):
            return web.json_response({"ok": False, "error": "短信验证码是 4-8 位数字"}, status=400)
        try:
            cookie = await app.sms.login(phone, code, str(d.get("captcha_key") or ""))
        except Exception as e:
            return web.json_response({"ok": False, "error": _sms_fail(e)}, status=400)
        app.schedule.disable_for_account_change()
        app.cfg["bili_cookie"] = cookie
        config.save(app.cfg)
        asyncio.create_task(app.broadcast.login())
        return ok(msg="登录成功")

    @routes.post("/api/bili/cookie")
    async def bili_cookie(request):
        if app.broadcast.running or app.broadcast.phase in ("starting", "stopping"):
            return web.json_response({"ok": False, "error": "请先停止直播，再切换主播账号"})
        app.schedule.disable_for_account_change()
        app.cfg["bili_cookie"] = (await request.json()).get("cookie", "").strip()
        config.save(app.cfg)
        asyncio.create_task(app.broadcast.login())
        return ok()

    @routes.post("/api/bili/logout")
    async def bili_logout(_):
        if app.broadcast.running or app.broadcast.phase in ("starting", "stopping"):
            return web.json_response({"ok": False, "error": "请先停止直播，再退出主播账号"})
        app.schedule.disable_for_account_change()
        app.cfg["bili_cookie"] = ""
        config.save(app.cfg)
        await app.bili.stop()
        app.bili.login_uid, app.bili.login_name = None, ""
        return ok()

    @routes.get("/api/broadcast/account")
    async def broadcast_account(_):
        result = await app.broadcast.account()
        app.room_cover_urls = [u for u in (result.get("cover"), result.get("cover_fallback")) if u]
        return web.json_response(result)

    @routes.get("/api/broadcast/room/cover.jpg")
    async def broadcast_room_cover_image(_):
        # B站封面链会失效或防盗链，这里由服务端带 Referer 代取，并回退到 keyframe。
        headers = {"Referer": "https://live.bilibili.com/", "User-Agent": "Mozilla/5.0"}
        async with ClientSession() as session:
            for url in getattr(app, "room_cover_urls", []):
                try:
                    async with session.get(url, headers=headers, timeout=8) as r:
                        if r.status != 200:
                            continue
                        data = await r.read()
                        if not data:
                            continue
                        return web.Response(body=data, content_type=r.headers.get("Content-Type", "image/jpeg"),
                                            headers={"Cache-Control": "max-age=60"})
                except Exception:
                    continue
        return web.Response(status=404)

    @routes.get("/api/broadcast/areas")
    async def broadcast_areas(_):
        return web.json_response(await app.broadcast.areas())

    @routes.get("/api/broadcast/sources")
    async def broadcast_sources(_):
        return web.json_response(await app.broadcast.sources())

    @routes.get("/api/broadcast/status")
    async def broadcast_status(_):
        return web.json_response({**app.broadcast.status(),
                                  "finishing": app.schedule.active_action == "farewell"})

    @routes.get("/api/broadcast/schedule")
    async def broadcast_schedule(_):
        return web.json_response(app.schedule.status())

    @routes.post("/api/broadcast/schedule")
    async def broadcast_schedule_save(request):
        try:
            return web.json_response(await app.schedule.save(await request.json()))
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)

    @routes.post("/api/broadcast/timer/start")
    async def broadcast_timer_start(request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("请设置停播倒计时分钟数")
            return web.json_response(await app.schedule.arm_external_timer(data.get("minutes")))
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)

    @routes.post("/api/broadcast/timer/cancel")
    async def broadcast_timer_cancel(_):
        app.cfg["broadcast"]["auto_stop_minutes"] = 0
        config.save(app.cfg)
        app.schedule.cancel_external_timer()
        app.schedule.refresh_auto_timer()
        return web.json_response(app.schedule.status())

    @routes.post("/api/broadcast/room/cover")
    async def broadcast_room_cover(request):
        if not request.content_type.startswith("multipart/"):
            return web.json_response({"ok": False, "error": "请选择封面图片文件"}, status=400)
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "cover":
            return web.json_response({"ok": False, "error": "上传字段无效"}, status=400)
        data = await field.read(decode=False)
        return web.json_response(await app.broadcast.update_cover(
            data, field.filename or "cover.jpg", field.headers.get("Content-Type", "image/jpeg")))

    @routes.post("/api/broadcast/room/notice")
    async def broadcast_room_notice(request):
        body = await request.json()
        notice = body.get("notice") if isinstance(body, dict) else None
        if not isinstance(notice, str):
            return web.json_response({"ok": False, "error": "公告必须为文本"}, status=400)
        return web.json_response(await app.broadcast.update_notice(notice))

    @routes.get("/api/broadcast/stream.mjpg")
    async def broadcast_stream(request):
        if not app.broadcast.capture.previewing:
            return web.Response(status=204)
        response = web.StreamResponse(headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=frame",
            "Cache-Control": "no-store"})
        await response.prepare(request)
        last_seq = 0
        try:
            while app.broadcast.capture.previewing:
                # 事件等待而不是轮询：30fps 生产 + 30fps 轮询会平均漏掉一半帧
                got = await asyncio.to_thread(app.broadcast.capture.wait_jpeg, last_seq, 0.5)
                if got is None:
                    continue
                last_seq, data = got
                header = ("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                          % len(data)).encode()
                await response.write(header + data + b"\r\n")
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            pass
        return response

    @routes.get("/api/broadcast/frame.jpg")
    async def broadcast_frame(_):
        image = app.broadcast.frame()
        if image is None:
            return web.Response(status=204)
        return web.Response(body=image, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @routes.post("/api/broadcast/preview")
    async def broadcast_preview(request):
        return web.json_response(await app.broadcast.preview(await request.json()))

    @routes.post("/api/broadcast/preview/stop")
    async def broadcast_preview_stop(_):
        return web.json_response(await app.broadcast.stop_preview())

    @routes.post("/api/broadcast/source")
    async def broadcast_source(request):
        return web.json_response(await app.broadcast.start_source(await request.json()))

    @routes.post("/api/broadcast/source/stop")
    async def broadcast_source_stop(_):
        return web.json_response(await app.broadcast.stop_source())

    async def _auto_stop_after(seconds, line):
        """开播计时到点：先让皮套把告别语说完，再停播。"""
        try:
            await asyncio.sleep(seconds)
            if not app.broadcast.running:
                return
            if line:
                job = app.pipe.say_job_id(line, "")
                for _ in range(50):  # 最多再等 25 秒，避免 TTS 出错把停播卡住
                    if app.pipe.spoken_done(job):
                        break
                    await asyncio.sleep(0.5)
                await asyncio.sleep(1.2)
            log.info("auto stop: 已播 %d 分钟，自动停播", seconds // 60)
            app.push("notice", {"text": "已到设定时长，自动停播"})
            await app.schedule.stop_now()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("auto stop failed")

    @routes.post("/api/broadcast/start")
    async def broadcast_start(request):
        result = await app.broadcast.start(await request.json())
        if result.get("ok"):
            app.cfg["room_id"] = result["room_id"]
            config.save(app.cfg)
            asyncio.create_task(app.connect())
            minutes = int(app.cfg.get("broadcast", {}).get("auto_stop_minutes") or 0)
            task = getattr(app, "auto_stop_task", None)
            if task and not task.done():
                task.cancel()
            if minutes > 0:
                line = (app.cfg.get("broadcast", {}).get("auto_stop_line") or "").strip()
                app.auto_stop_task = asyncio.create_task(_auto_stop_after(minutes * 60, line))
                log.info("auto stop armed: %d 分钟后停播", minutes)
        return web.json_response(result)

    @routes.post("/api/broadcast/stop")
    async def broadcast_stop(_):
        task = getattr(app, "auto_stop_task", None)
        if task and not task.done():
            task.cancel()
        return web.json_response(await app.schedule.stop_now())

    @routes.post("/api/broadcast/finish")
    async def broadcast_finish(_):
        task = getattr(app, "auto_stop_task", None)
        if task and not task.done():
            task.cancel()
        return web.json_response(await app.schedule.stop_now(
            farewell="今天播了很久了，下播吃饭饭了。"))

    @routes.post("/api/test_event")
    async def test_event(request):
        ev = await request.json()
        ev.update({"test": True, "ts": time.time(), "uid": 0})
        for k, d in (("num", 1), ("value", 0.0), ("price", 30)):
            if k in ev:
                ev[k] = type(d)(ev[k] or d)
        if ev["type"] == "gift":
            ev["paid"] = ev.get("value", 0) > 0
        if ev["type"] == "superchat":
            ev["value"] = float(ev.get("price", 30))
        app.on_event(ev)
        return ok()

    @routes.post("/api/say")
    async def say(request):
        d = await request.json()
        text = (d.get("text") or "").strip()
        if not text:
            return ok()
        if d.get("ai"):
            try:
                text, emotion = await app.brain.ai(app.cfg, text)
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)})
        else:
            emotion = ""
        app.pipe.say(text, emotion)
        return ok(text=text)

    @routes.post("/api/ai/test")
    async def ai_test(request):
        prompt = (await request.json()).get("prompt", "")
        t0 = time.time()
        try:
            text, emotion = await app.brain.ai(app.cfg, prompt)
            return ok(text=text, emotion=emotion, seconds=round(time.time() - t0, 1))
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    @routes.post("/api/ai/reset")
    async def ai_reset(_):
        await app.brain.close()
        app.brain.openai.history.clear()
        return ok()

    @routes.get("/api/ai/models")
    async def ai_models(_):
        ai = app.cfg["ai"]
        try:
            if ai.get("backend") == "openai":
                models = await app.brain.openai.list_models(ai)
            else:
                models = await list_qoder_models(ai)
            return ok(models=models)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e) or type(e).__name__})

    # ---------- 心愿单达成动作 ----------
    @routes.get("/api/wishlist/state")
    async def wishlist_state(_):
        return web.json_response(app.wishlist.state())

    @routes.post("/api/wishlist/test")
    async def wishlist_test(_):
        return web.json_response(await app.wishlist.test())

    # ---------- 点歌 ----------
    song_login = {"generation": 0, "key": None}

    def reset_song_login():
        song_login["generation"] += 1
        song_login["key"] = None
        app.songs.ne.cancel_qr()
        return song_login["generation"]

    @routes.get("/songs")
    async def songs_page(_):
        return page("songs.html")

    @routes.get("/api/songs/state")
    async def songs_state(_):
        return web.json_response(app.songs.state())

    @routes.post("/api/songs/control")
    async def songs_control(request):
        d = await request.json()
        app.songs.control(d.get("action"), d.get("qid"))
        return ok()

    @routes.post("/api/songs/search")
    async def songs_search(request):
        q = ((await request.json()).get("q") or "").strip()
        try:
            return ok(songs=await app.songs.ne.search_with_status(q) if q else [])
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    @routes.post("/api/songs/add")
    async def songs_add(request):
        d = await request.json()
        app.songs.add(d["song"], d.get("by") or "主播", "__host__")
        return ok()

    @routes.post("/api/songs/give")
    async def songs_give(request):
        d = await request.json()
        app.songs.give(d.get("key"), int(d.get("song", 0)), int(d.get("pin", 0)))
        return ok()

    @routes.post("/api/songs/qr")
    async def songs_qr(_):
        import qrcode
        import qrcode.image.svg
        generation = reset_song_login()
        try:
            key, url = await app.songs.ne.qr_key()
            if generation != song_login["generation"]:
                raise RuntimeError("二维码已更新，请使用最新二维码")
            song_login["key"] = key
            img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
            return ok(svg=img.to_string(encoding="unicode"), key=key, captcha_id=CAPTCHA_ID, device_app_id=DEVICE_APP_ID)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e) or "生成网易云二维码失败，请重试"})

    @routes.post("/api/songs/qrpoll")
    async def songs_qrpoll(request):
        try:
            data = await request.json()
            key = data.get("key")
            generation = song_login["generation"]
            if not key or key != song_login["key"]:
                raise RuntimeError("二维码已更新或已结束，请重新扫码")
            proof = {field: data[field] for field in ("secure_captcha", "yd_device_token") if field in data}
            code, msg, cookie = await app.songs.ne.qr_poll(key, **proof)
            if generation != song_login["generation"] or key != song_login["key"]:
                raise RuntimeError("二维码已更新或已结束，请重新扫码")
            if code == 803:
                if not cookie:
                    raise RuntimeError("网易云未返回登录凭证，请重新扫码")
                song_login["key"] = None
                app.cfg["songs"]["netease_cookie"] = cookie
                config.save(app.cfg)
                await app.songs.refresh_account()
                if not app.songs.account:
                    msg = "登录信息已保存；" + (app.songs.account_error or "暂未获取到账号信息，请稍后重试")
            elif code == 800:
                song_login["key"] = None
            return ok(code=code, msg=msg, account=app.songs.account if code == 803 else None)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e) or "查询网易云扫码状态失败，请重试"})

    @routes.post("/api/songs/cookie")
    async def songs_cookie(request):
        reset_song_login()
        app.cfg["songs"]["netease_cookie"] = ((await request.json()).get("cookie") or "").strip()
        config.save(app.cfg)
        await app.songs.refresh_account()
        return ok(account=app.songs.account)

    @routes.post("/api/tts/start")
    async def tts_start(_):
        asyncio.create_task(app.engine.start(app.cfg["tts"], lambda m: app.push("log", {"level": "info", "msg": m})))
        return ok()

    @routes.post("/api/tts/stop")
    async def tts_stop(_):
        if app.engine.proc:
            app.engine.stop()
            app.push("log", {"level": "info", "msg": "已关闭 GPT-SoVITS 引擎"})
            return ok()
        return web.json_response({"ok": False, "error": "引擎不是本软件启动的，请在它自己的窗口关闭"})

    @routes.get("/api/tts/weights")
    async def tts_weights(_):
        gpt, sovits = app.engine.list_weights(app.cfg["tts"]["gsv_dir"])
        return web.json_response({"gpt": gpt, "sovits": sovits})

    @routes.get("/api/tts/log")
    async def tts_log(_):
        return web.Response(text=app.engine.tail_log(80))

    @routes.get("/api/audio/devices")
    async def devices(_):
        return web.json_response(Player.devices())

    @routes.post("/api/control")
    async def control(request):
        action = (await request.json()).get("action")
        if action == "pause":
            app.pipe.paused = True
        elif action == "resume":
            app.pipe.paused = False
        elif action == "skip":
            app.player.skip()
        elif action == "clear":
            app.pipe.clear()
        app.push("log", {"level": "info", "msg": {"pause": "已暂停自动回复", "resume": "已恢复自动回复",
                                                  "skip": "已跳过当前语音", "clear": "已清空待回复队列"}.get(action, action)})
        return ok()

    @routes.post("/api/pick")
    async def pick(request):
        d = await request.json()
        path = await asyncio.get_running_loop().run_in_executor(None, pick_path, d.get("kind", "file"), d.get("initial", ""))
        return ok(path=(path or "").replace("/", "\\"))

    @routes.post("/api/quit")
    async def quit_(_):
        app.schedule.closing = app.schedule.abort_start = app.schedule.stop_requested = True
        asyncio.get_running_loop().call_later(0.3, app.stop_event.set)
        return ok()

    # ---------- 游戏自动托管 ----------
    @routes.get("/api/desktop/status")
    async def desktop_status(_):
        from dtgame import ttsd
        tts_cfg = app.cfg["tts"]
        if tts_cfg.get("stream_enabled") and tts_cfg.get("qwen_enabled"):
            engine_ready = await ttsd.alive(tts_cfg)
        else:
            engine_ready = app.engine_ready.is_set()
        return web.json_response({
            "app": "WhaleTalk", "running": bool(app.dt and app.dt.running),
            "starting": bool(app.dt and not app.engine_ready.is_set()),
            "engine_ready": engine_ready, "game_running": app.game.running(),
            "connected": await asyncio.to_thread(app._bridge_alive),
        })

    @routes.post("/api/dtgame/resume")
    async def dt_resume(request):
        data = await request.json()
        if not isinstance(data, dict) or not isinstance(data.get("new_run", False), bool):
            return web.json_response({"ok": False, "error": "new_run 必须为布尔值"}, status=400)
        return web.json_response(await app.resume_dt(data.get("new_run", False)))

    @routes.post("/api/dtgame/start")
    async def dt_start(_):
        return web.json_response(app.start_dt())

    @routes.post("/api/dtgame/stop")
    async def dt_stop(_):
        return web.json_response(await app.stop_dt())

    @routes.get("/api/dtgame/status")
    async def dt_status(_):
        running = bool(app.dt and app.dt.running)
        c = app.cfg.get("dtgame", {})
        info = {
            "running": running,
            "turns": app.dt.turns_taken if app.dt else 0,
            "game_running": app.game.running(),
            "auto_launch": bool(c.get("auto_launch", True)),
            "godot": find_godot(str(c.get("godot_exe", ""))),
            "project_dir": c.get("project_dir", ""),
        }
        why = app._config_problem()
        try:
            info["game"] = await asyncio.to_thread(app._dt_bridge().screen)
            info["ready"] = not why
            info["why"] = why
        except BridgeError as error:
            info["game"] = {}
            info["ready"] = False
            info["why"] = why or str(error)
        return web.json_response(info)

    @routes.post("/api/dtgame/launch")
    async def dt_launch(_):
        if await asyncio.to_thread(app._bridge_alive):
            return web.json_response({"ok": True, "already": True})
        res = await app.game.launch_and_wait(app._bridge_alive)
        if res.get("ok"):
            app.push("log", {"level": "info", "msg": "游戏已启动"})
        return web.json_response(res)

    @routes.post("/api/dtgame/stopgame")
    async def dt_stopgame(_):
        ok = app.game.stop()
        return web.json_response({"ok": ok})

    wapp = web.Application(middlewares=[guard], client_max_size=MAX_BODY_BYTES)
    wapp.add_routes(routes)
    wapp.router.add_static("/static", config.WEB_DIR)
    return wapp


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.exists(p):
                return p
    return None


def open_ui(url):
    edge = find_edge()
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--window-size=1320,900"])
    else:
        webbrowser.open(url)


def port_in_use(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


async def amain(app: App):
    app.loop = asyncio.get_running_loop()
    app.stop_event = asyncio.Event()
    port = int(app.cfg.get("web_port", 8787))
    runner = web.AppRunner(build_web(app), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port, reuse_address=False).start()
    url = f"http://127.0.0.1:{port}/"
    log.info("界面地址：%s    OBS 字幕：%soverlay", url, url)
    await app.startup()
    if app.cfg.get("open_ui", True) and "--no-ui" not in sys.argv:
        open_ui(url)
    try:
        await app.stop_event.wait()
    finally:
        log.info("正在退出…")
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.ensure_future(app.shutdown())), 120)
        except (Exception, asyncio.CancelledError) as e:
            log.warning("退出清理未完全完成：%r", e)
        try:
            await asyncio.wait_for(runner.cleanup(), 1)
        except (Exception, asyncio.CancelledError):
            pass
        log.info("已退出")


def main():
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetConsoleTitleW("千音ai直播工具 - 关闭此窗口即退出")
    cfg = config.load()
    port = int(cfg.get("web_port", 8787))
    if port_in_use(port):
        print("程序已经在运行，直接打开界面。")
        open_ui(f"http://127.0.0.1:{port}/")
        return
    print("=" * 60)
    print(" 千音ai直播工具  ·  B站直播 AI 语音助手   关闭此窗口即退出程序")
    print("=" * 60)
    app = App()
    try:
        asyncio.run(amain(app))
    except KeyboardInterrupt:
        pass
    logging.shutdown()
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
