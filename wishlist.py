"""直播心愿单达成后的动作；通过现有播放和播报队列执行。"""
import asyncio
import copy
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from brain import fill

CHINA = timezone(timedelta(hours=8))
ACTIONS = {"audio": "播放 AI 翻唱 / 本地音频", "song": "播放网易云歌曲",
           "say": "朗读自定义台词", "ai": "按提示让 AI 发言"}


def validate_settings(settings, *, executing=False):
    if type(settings.get("enabled")) is not bool:
        raise ValueError("请设置是否启用心愿单达成动作")
    action = settings.get("action")
    if action not in ACTIONS:
        raise ValueError("请选择有效的心愿单达成动作")
    for field in ("audio_file", "song_query", "text", "prompt", "emotion"):
        if not isinstance(settings.get(field, ""), str) or len(settings.get(field, "")) > 4000:
            raise ValueError("心愿单动作参数必须是 4000 字以内的文本")
    if not (settings["enabled"] or executing):
        return
    if action == "audio":
        filename = Path(settings.get("audio_file", "")).expanduser()
        if not filename.is_file() or filename.suffix.lower() not in (".wav", ".mp3", ".flac", ".ogg"):
            raise ValueError("请先选择有效的 AI 翻唱或本地音频文件（WAV / MP3 / FLAC / OGG）")
    field = {"song": "song_query", "say": "text", "ai": "prompt"}.get(action)
    if field and not settings.get(field, "").strip():
        raise ValueError({"song": "请填写要播放的歌名和歌手", "say": "请填写自定义台词", "ai": "请填写 AI 提示词"}[action])


class WishlistActions:
    def __init__(self, app, clock=time.time):
        self.app, self.clock = app, clock
        self.latest = {}
        self.attempted = set()
        self.jobs = set()
        self.lock = asyncio.Lock()
        self.busy = False
        self.closing = False
        self.last_result = None

    @property
    def settings(self):
        return {**config.DEFAULTS["wishlist"], **self.app.cfg.get("wishlist", {})}

    def room(self):
        bili = getattr(self.app, "bili", None)
        client = getattr(bili, "client", None)
        return str(getattr(client, "room_id", None) or getattr(bili, "room_id", None)
                   or self.app.cfg.get("room_id") or "")

    def state(self):
        return {"enabled": self.settings["enabled"], "action": self.settings["action"],
                "busy": self.busy or bool(self.jobs), "latest": self.latest.get(self.room()),
                "last_result": self.last_result}

    def push(self):
        self.app.push("wishlist", self.state())

    def _key(self, ev):
        room, wish_id = str(ev.get("room_id") or ""), str(ev.get("wish_id") or "")
        if not room.isdigit() or int(room) <= 0 or not wish_id.isdigit() or int(wish_id) <= 0:
            return None
        today = datetime.fromtimestamp(self.clock(), CHINA).date().isoformat()
        # 有服务端时间时拒绝旧日状态回放；无时间的进度消息按接收当天归属。
        if ev.get("wish_ts") is not None:
            try:
                day = datetime.fromtimestamp(float(ev["wish_ts"]), CHINA).date().isoformat()
            except (ValueError, TypeError, OverflowError, OSError):
                return None
            if day != today:
                return None
        return f"{today}:{room}:{wish_id}"

    def _remember(self, key):
        settings = self.app.cfg.setdefault("wishlist", {})
        previous = list(settings.get("_completed") or [])
        settings["_completed"] = [*previous, key][-512:]
        try:
            config.save(self.app.cfg)
        except Exception:
            settings["_completed"] = previous
            raise

    def _forget(self, key):
        settings = self.app.cfg.setdefault("wishlist", {})
        settings["_completed"] = [k for k in settings.get("_completed") or [] if k != key]
        config.save(self.app.cfg)

    def on_event(self, ev):
        room = str(ev.get("room_id") or "")
        if room != self.room() or self.closing:
            return
        self.latest[room] = dict(ev)
        self.push()
        if ev.get("status") != 2 or not self.settings["enabled"]:
            return
        key = self._key(ev)
        if not key or key in self.attempted or key in (self.settings.get("_completed") or []):
            return
        if len(self.jobs) >= 8:
            self._result(False, "心愿单动作等待过多，请稍后手动测试动作")
            return
        self.attempted.add(key)
        settings = copy.deepcopy(self.settings)
        try:
            validate_settings(settings, executing=True)
            # 在异步生成 / 搜歌之前落盘，避免重复推送和重启造成重复表演。
            self._remember(key)
        except Exception as error:
            self._result(False, str(error) or "心愿单动作准备失败")
            return
        self._spawn(dict(ev), settings, key)

    def _spawn(self, ev, settings, key=None):
        task = asyncio.create_task(self._run(ev, settings, key))
        self.jobs.add(task)
        def finished(job):
            self.jobs.discard(job)
            if not self.closing:
                self.push()
        task.add_done_callback(finished)
        return task

    def _result(self, ok, message):
        self.last_result = {"ok": ok, "message": message,
                            "time": datetime.fromtimestamp(self.clock(), CHINA).strftime("%H:%M:%S")}
        self.app.push("log", {"level": "info" if ok else "error", "msg": "心愿单：" + message})
        self.push()

    async def _perform(self, ev, settings):
        validate_settings(settings, executing=True)
        variables = {"wish": ev.get("wish_name") or "直播心愿单", "room": str(ev.get("room_id") or self.room())}
        emotion = settings.get("emotion", "开心")
        action = settings["action"]
        if action == "audio":
            filename = str(Path(settings["audio_file"]).expanduser().resolve())
            job = self.app.pipe.play_audio(filename, f"心愿达成 · {Path(filename).stem}", emotion)
            if job is None:
                raise RuntimeError("播报队列已满，音频未能加入队列")
            return "AI 翻唱 / 本地音频已加入播报队列"
        if action == "song":
            if not self.app.cfg["songs"].get("enabled"):
                raise RuntimeError("请先启用点歌，再使用网易云歌曲动作")
            song = await self.app.songs.ne.find(settings["song_query"].strip(),
                                               int(float(self.app.cfg["songs"].get("max_minutes", 8)) * 60))
            if not song:
                raise RuntimeError("没有找到能完整播放的歌曲，请换首歌或登录网易云账号")
            self.app.songs.add(song, "心愿单奖励", "__wishlist__", settings["song_query"])
            return f"《{song['name']}》已加入点歌队列"
        text = fill(settings["text"] if action == "say" else settings["prompt"], variables)
        if action == "ai":
            text, detected = await self.app.brain.ai(self.app.cfg, text)
            emotion = detected or emotion
        if not text or not text.strip():
            raise RuntimeError("AI 未返回可播报的内容，请修改提示词后重试")
        if self.app.pipe.say(text, emotion) is None:
            raise RuntimeError("播报队列已满，台词未能加入队列")
        return "自定义台词已加入播报队列"

    async def _run(self, ev, settings, key):
        async with self.lock:
            self.busy = True
            self.push()
            try:
                message = await asyncio.wait_for(self._perform(ev, settings), 60)
                self._result(True, message)
                return {"ok": True, "message": message}
            except Exception as error:
                if key:
                    try:
                        self._forget(key)
                    except Exception:
                        pass
                message = str(error) or ("动作执行超时，请检查网络或 AI 服务" if isinstance(error, TimeoutError) else "动作执行失败")
                self._result(False, message)
                return {"ok": False, "error": message}
            finally:
                self.busy = False
                self.push()

    async def test(self):
        if self.closing or self.jobs:
            return {"ok": False, "error": "心愿单动作正在处理，请稍后再试"}
        settings = copy.deepcopy(self.settings)
        try:
            validate_settings(settings, executing=True)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        ev = {"wish_name": "测试心愿单", "room_id": self.room()}
        return await self._spawn(ev, settings)

    async def close(self):
        self.closing = True
        for job in list(self.jobs):
            job.cancel()
        await asyncio.gather(*list(self.jobs), return_exceptions=True)
