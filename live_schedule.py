import asyncio
import re
from datetime import datetime, timedelta

import config

_CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")


class DailyBroadcastSchedule:
    # 刚启动的游戏窗口要一两秒才出现在枚举里，重试这么多秒（测试里会调小）
    WINDOW_TRIES = 10
    FAREWELL_TIMEOUT = 90
    FAREWELL_TAIL = 1.2
    FAREWELL_LEAD_SECONDS = 30
    TIMER_SECONDS_PER_MINUTE = 60
    AUTO_CHECK_SECONDS = 15

    def __init__(self, app, clock=datetime.now):
        self.app = app
        self.clock = clock
        self.last_minute = self.clock().strftime("%Y-%m-%d %H:%M")
        self.loop_task = None
        self.jobs = set()
        self.execution_lock = asyncio.Lock()
        self.abort_start = False
        self.stop_requested = False
        self.closing = False
        self.last_result = None
        self.active_action = ""
        self.daily_stop_task = None
        self.external_timer_task = None
        self.timer_deadline = None
        self.timer_room_id = 0
        self.timer_revision = 0
        self.timer_automatic = False
        self.auto_check_task = None
        self.auto_last_check = None
        self.auto_minutes = None
        self.auto_session = None
        self.auto_session_started = None
        self.auto_handled = None
        self.auto_message = ""

    @property
    def settings(self):
        return self.app.cfg["broadcast"]["schedule"]

    def within_broadcast_window(self):
        if self.settings.get("mode", "managed") == "stop_only":
            return False
        current = self.clock().strftime("%H:%M")
        start, stop = self.settings["start_time"], self.settings["stop_time"]
        return start <= current < stop if start < stop else current >= start or current < stop

    async def save(self, data):
        if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
            raise ValueError("请设置是否启用每日直播计划")
        mode = data.get("mode", self.settings.get("mode", "managed"))
        if mode not in ("managed", "stop_only"):
            raise ValueError("请选择自动开停播或直播姬仅定时停播")
        start = data.get("start_time", self.settings["start_time"])
        stop = data.get("stop_time")
        if not isinstance(start, str) or not _CLOCK.fullmatch(start) or not isinstance(stop, str) or not _CLOCK.fullmatch(stop):
            raise ValueError("开播和停播时间必须为 HH:MM 格式")
        if start == stop and mode == "managed":
            raise ValueError("开播和停播时间不能相同")
        updated = {**self.settings, "enabled": data["enabled"], "mode": mode, "start_time": start, "stop_time": stop}
        title, area = self.app.cfg["broadcast"]["title"], self.app.cfg["broadcast"]["area_id"]
        if data["enabled"]:
            if data.get("authorized") is not True:
                raise ValueError("启用前需要确认允许每日自动停播" if mode == "stop_only" else "启用前需要确认允许每日自动公开开播")
            if not self.app.cfg.get("bili_cookie"):
                raise ValueError("请先扫码登录B站主播账号")
        if data["enabled"] and mode == "stop_only":
            info = await self.app.broadcast.account()
            if not info.get("ok") or not info.get("logged_in") or not info.get("room_id"):
                raise ValueError(info.get("error") or "请先扫码登录正在直播的主播账号")
            updated["external_room_id"] = int(info["room_id"])
        elif data["enabled"]:
            title, area = data.get("title"), data.get("area_id")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 60 or type(area) is not int or area <= 0:
                raise ValueError("请先填写直播标题并选择分区")
            hwnd, audio = self.app.broadcast.selection(data)
            capture = self.app.broadcast.capture
            if not capture.previewing or capture.hwnd != hwnd or capture.audio_device != audio or capture.error:
                raise ValueError("请先预览选中的游戏窗口与声音，再保存启用计划")
            sources = await self.app.broadcast.sources()
            if not sources.get("ok"):
                raise ValueError(sources.get("error") or "无法读取直播采集源")
            windows = [w for w in sources["windows"] if w["hwnd"] == hwnd]
            devices = [a for a in sources["audio"] if a["id"] == audio]
            if len(windows) != 1 or len(devices) != 1:
                raise ValueError("选中的采集源已变化，请刷新并重新预览")
            window_title, audio_name = windows[0]["title"], devices[0]["name"]
            if sum(w["title"] == window_title for w in sources["windows"]) != 1 or sum(a["name"] == audio_name for a in sources["audio"]) != 1:
                raise ValueError("窗口或声音设备存在重名，请关闭重复窗口后再保存计划")
            updated.update(window_title=window_title, audio_name=audio_name)
        if not data["enabled"] or mode == "stop_only":
            self.abort_start = True
        if self._cancel_daily_farewell():
            updated["last_stop"] = ""
        self.app.cfg["broadcast"].update(schedule=updated, title=title.strip(), area_id=area)
        config.save(self.app.cfg)
        self.last_minute = self.clock().strftime("%Y-%m-%d %H:%M")
        self.last_result = None
        return self.status()

    def status(self):
        now = self.clock()
        settings = dict(self.settings)
        upcoming = {}
        if settings["enabled"]:
            for action in (("stop",) if settings.get("mode") == "stop_only" else ("start", "stop")):
                hour, minute = map(int, settings[action + "_time"].split(":"))
                next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_time <= now or settings["last_" + action] == next_time.date().isoformat():
                    next_time += timedelta(days=1)
                upcoming["next_" + action] = next_time.strftime("%Y-%m-%d %H:%M")
        return {"ok": True, **settings, **upcoming, "last_result": self.last_result,
                "active_action": self.active_action, "local_time": now.strftime("%Y-%m-%d %H:%M"),
                "external_timer": {"enabled": self.timer_deadline is not None,
                                   "auto_minutes": self.app.cfg["broadcast"].get("auto_stop_minutes", 0),
                                   "message": self.auto_message,
                                   "room_id": self.timer_room_id,
                                   "deadline": self.timer_deadline.strftime("%Y-%m-%d %H:%M:%S") if self.timer_deadline else "",
                                   "remaining_seconds": max(0, int((self.timer_deadline - now).total_seconds())) if self.timer_deadline else 0}}

    def disable_for_account_change(self):
        self.cancel_external_timer()
        if self._cancel_daily_farewell():
            self.settings["last_stop"] = ""
        self.settings["enabled"] = False
        self.abort_start = True
        self.last_result = {"action": "disabled", "time": self.clock().strftime("%Y-%m-%d %H:%M"),
                            "ok": True, "message": "B站账号已变化，定时直播已关闭，请重新确认启用"}

    def start(self):
        self.loop_task = asyncio.create_task(self.run())

    async def run(self):
        while not self.closing:
            self.tick()
            await asyncio.sleep(5)

    def tick(self):
        self.refresh_auto_timer()
        now = self.clock()
        minute = now.strftime("%Y-%m-%d %H:%M")
        # tick 每五秒运行一次，不能只在分钟变化时检查，否则会错过提前告别。
        if not self.closing and self.settings["enabled"]:
            hour, stop_minute = map(int, self.settings["stop_time"].split(":"))
            deadline = now.replace(hour=hour, minute=stop_minute, second=0, microsecond=0)
            if deadline < now:
                deadline += timedelta(days=1)
            remaining = (deadline - now).total_seconds()
            day = deadline.date().isoformat()
            if 0 < remaining <= self.FAREWELL_LEAD_SECONDS and self.settings["last_stop"] != day:
                self.settings["last_stop"] = day
                config.save(self.app.cfg)
                self.abort_start = self.stop_requested = True
                job = asyncio.create_task(self.execute("stop", wait_seconds=remaining))
                self.daily_stop_task = job
                self.jobs.add(job)
                job.add_done_callback(self.jobs.discard)
        if self.closing or minute == self.last_minute:
            return
        self.last_minute = minute
        if not self.settings["enabled"]:
            return
        for action in (("stop",) if self.settings.get("mode") == "stop_only" else ("start", "stop")):
            if now.strftime("%H:%M") != self.settings[action + "_time"]:
                continue
            day = now.date().isoformat()
            if self.settings["last_" + action] == day:
                continue
            self.settings["last_" + action] = day
            config.save(self.app.cfg)
            self.abort_start = self.stop_requested = action == "stop"
            job = asyncio.create_task(self.execute(action))
            if action == "stop":
                self.daily_stop_task = job
            self.jobs.add(job)
            job.add_done_callback(self.jobs.discard)

    async def execute(self, action, *, wait_seconds=0):
        async with self.execution_lock:
            if self.closing or not self.settings["enabled"]:
                return
            if action == "start" and self.settings.get("mode") == "stop_only":
                return
            self.active_action = action
            try:
                if action == "stop":
                    self.cancel_external_timer()
                    self._cancel_local_timer()
                    result = await self._stop_broadcast(external=self.settings.get("mode") == "stop_only",
                                                        room_id=self.settings.get("external_room_id", 0),
                                                        farewell=self.farewell_line(), wait_seconds=wait_seconds,
                                                        stop_on_farewell_error=True)
                elif self.app.broadcast.running or self.app.broadcast.phase == "starting":
                    result = {"ok": True, "message": "已在直播，跳过重复开播"}
                else:
                    result = await self.start_broadcast()
                if result.get("ok"):
                    message = result.get("message") or ("定时开播已完成" if action == "start" else "定时停播已完成")
                else:
                    message = result.get("error") or "定时直播操作失败"
                self.last_result = {"action": action, "time": self.clock().strftime("%Y-%m-%d %H:%M"),
                                    "ok": bool(result.get("ok")), "message": message}
                self.app.push("log", {"level": "info" if result.get("ok") else "warn", "msg": message})
            except Exception:
                self.last_result = {"action": action, "time": self.clock().strftime("%Y-%m-%d %H:%M"),
                                    "ok": False, "message": "定时直播执行失败，请检查账号与采集源"}
                self.app.push("log", {"level": "warn", "msg": self.last_result["message"]})
            finally:
                self.active_action = ""

    async def start_broadcast(self):
        prep = await self.app.ensure_game_and_dt()
        if not prep.get("ok"):
            return {"ok": False, "error": prep.get("error") or "定时开播未执行：游戏没能启动"}
        # 刚把游戏拉起来时窗口要一两秒才出现在枚举里；本来就在跑就不用白等
        for attempt in range(self.WINDOW_TRIES if prep.get("launched") else 1):
            sources = await self.app.broadcast.sources()
            windows = [w for w in sources.get("windows", []) if w["title"] == self.settings["window_title"]]
            devices = [a for a in sources.get("audio", []) if a["name"] == self.settings["audio_name"]]
            if sources.get("ok") and len(windows) == 1 and len(devices) == 1:
                break
            if self.closing or self.abort_start or not self.settings["enabled"]:
                return {"ok": True, "message": "已取消本次定时开播"}
            if attempt + 1 < self.WINDOW_TRIES:
                await asyncio.sleep(1)
        else:
            return {"ok": False, "error": "定时开播未执行：游戏窗口或声音设备不存在、已最小化或存在重名"}
        if self.closing or self.abort_start or not self.settings["enabled"] or not self.within_broadcast_window():
            return {"ok": True, "message": "已取消本次定时开播"}
        selection = {"hwnd": windows[0]["hwnd"], "audio_device": devices[0]["id"]}
        preview = await self.app.broadcast.preview(selection)
        if not preview.get("ok"):
            return preview
        if self.closing or self.abort_start or not self.settings["enabled"] or not self.within_broadcast_window():
            await self.app.broadcast.stop_preview()
            return {"ok": True, "message": "已取消本次定时开播"}
        result = await self.app.broadcast.start({**selection, "title": self.app.cfg["broadcast"]["title"],
                                                  "area_id": self.app.cfg["broadcast"]["area_id"], "confirm": True})
        if result.get("ok"):
            if self.closing or self.stop_requested:
                return {"ok": True, "message": "已请求取消本次定时开播，等待停止完成"}
            if self.abort_start or not self.settings["enabled"] or not self.within_broadcast_window():
                self.settings["last_stop"] = self.clock().date().isoformat()
                config.save(self.app.cfg)
                stopped = await self.app.broadcast.stop()
                return {"ok": True, "message": "已取消本次定时开播并停止推流"} if stopped.get("ok") else stopped
            self.app.cfg["room_id"] = result["room_id"]
            config.save(self.app.cfg)
            asyncio.create_task(self.app.connect())
            self.arm_local_timer()
        return result

    def farewell_line(self):
        line = str(self.app.cfg["broadcast"].get("auto_stop_line") or "").strip()
        return line or config.DEFAULTS["broadcast"]["auto_stop_line"]

    def _cancel_daily_farewell(self):
        task = self.daily_stop_task
        if task and task is not asyncio.current_task() and not task.done() and self.active_action == "farewell":
            task.cancel()
            return True
        return False

    def _cancel_local_timer(self):
        task = getattr(self.app, "auto_stop_task", None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _say_farewell(self, farewell, stop_on_error):
        self.active_action = "farewell"
        self.app.push("notice", {"text": "正在告别，说完后自动下播"})
        job = self.app.pipe.say_job_id(farewell, "", farewell=True)
        error = ""
        if job is None:
            error = "告别语音未能加入队列，请重试手动下播"
        else:
            try:
                async with asyncio.timeout(self.FAREWELL_TIMEOUT):
                    while not self.app.pipe.spoken_done(job):
                        await asyncio.sleep(0.1)
            except TimeoutError:
                error = "告别语音等待超时，直播尚未停止；可重试手动下播，或点击停止直播"
            else:
                await asyncio.sleep(self.FAREWELL_TAIL)
        if error:
            if not stop_on_error:
                return {"ok": False, "error": error}
            self.app.push("log", {"level": "warn", "msg": error + "；继续执行定时停播"})
        return None

    async def _stop_broadcast(self, *, external=False, room_id=0, farewell="", wait_seconds=0,
                              stop_on_farewell_error=False):
        deadline = asyncio.get_running_loop().time() + wait_seconds
        local = self.app.broadcast.running or self.app.broadcast.phase in ("starting", "stopping")
        live = self.app.broadcast.running
        if not local and (external or self.app.cfg.get("bili_cookie")):
            info = await self.app.broadcast.account()
            if not info.get("ok") or not info.get("logged_in"):
                return {"ok": False, "error": info.get("error") or "无法确认主播直播状态，请重新登录"}
            if external and int(info.get("room_id") or 0) != room_id:
                return {"ok": False, "error": "主播账号或直播间已变化，请重新设置停播倒计时"}
            room_id = int(info.get("room_id") or 0)
            live = int(info.get("live_status") or 0) == 1 and bool(room_id)
        if farewell and live:
            error = await self._say_farewell(farewell, stop_on_farewell_error)
            if error:
                return error
        # 提前告别，但保留原定停止时刻；合成较慢时优先让最后一句说完。
        await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        if local:
            return await self.app.broadcast.stop()
        if live or external:
            return await self.app.broadcast.stop_external(room_id)
        return {"ok": True, "message": "当前未在直播，无需定时停播"}

    def arm_local_timer(self):
        self._cancel_local_timer()
        minutes = self.app.cfg["broadcast"].get("auto_stop_minutes", 0)
        self.app.auto_stop_task = (asyncio.create_task(self._local_stop_after(minutes * self.TIMER_SECONDS_PER_MINUTE))
                                   if minutes > 0 else None)

    async def _local_stop_after(self, seconds):
        try:
            lead = min(seconds, self.FAREWELL_LEAD_SECONDS * self.TIMER_SECONDS_PER_MINUTE / 60)
            await asyncio.sleep(max(0, seconds - lead))
            if self.app.broadcast.running:
                await self.stop_now(farewell=self.farewell_line(), wait_seconds=lead, stop_on_farewell_error=True)
        except asyncio.CancelledError:
            pass
        except Exception:
            self.app.push("log", {"level": "warn", "msg": "自动停播失败，请检查直播状态"})

    def refresh_auto_timer(self):
        """保存时和后台定时检查都调用，不依赖网页、手动按钮或本工具推流。"""
        if self.closing:
            return
        minutes = self.app.cfg["broadcast"].get("auto_stop_minutes", 0)
        if type(minutes) is not int or not 0 <= minutes <= 10080:
            self.auto_message = "请将自动停播时长设置为 0 至 10080 分钟的整数"
            return
        if minutes != self.auto_minutes:
            if self.timer_automatic:
                self.cancel_external_timer()
            self.auto_minutes = minutes
            self.auto_last_check = None
            self.auto_handled = None
        if not minutes:
            self._cancel_local_timer()
            self.auto_message = "自动停播已关闭，设置分钟数即可启用"
            return
        if self.app.broadcast.running or self.app.broadcast.phase in ("starting", "stopping"):
            if self.timer_automatic:
                self.cancel_external_timer()
            self.auto_message = "本工具推流中，使用开播时的自动停播计时"
            return
        if not self.app.cfg.get("bili_cookie"):
            self.auto_message = "请登录主播账号，登录后自动监测开播"
            return
        if self.auto_check_task and not self.auto_check_task.done():
            return
        now = self.clock()
        if self.auto_last_check is not None and 0 <= (now - self.auto_last_check).total_seconds() < self.AUTO_CHECK_SECONDS:
            return
        self.auto_last_check = now
        self.auto_check_task = asyncio.create_task(self._check_external_live(
            minutes, self.timer_revision, self.app.cfg.get("bili_cookie")))

    async def _check_external_live(self, minutes, revision, cookie):
        try:
            info = await self.app.broadcast.account()
            if (self.closing or revision != self.timer_revision or minutes != self.auto_minutes
                    or cookie != self.app.cfg.get("bili_cookie")):
                return
            if not info.get("ok") or not info.get("logged_in") or not info.get("room_id"):
                self.auto_message = info.get("error") or "请登录主播账号，登录后自动监测开播"
                return
            if int(info.get("live_status") or 0) != 1:
                if self.timer_automatic:
                    self.cancel_external_timer()
                self.auto_session = self.auto_session_started = self.auto_handled = None
                self.auto_message = "已开启自动停播，等待直播姬 / OBS 开播"
                return
            room_id = int(info["room_id"])
            started_at = float(info.get("live_started_at") or 0)
            # 有平台开播时间时，软件重启也沿用本场直播的时间；无此字段才从首次检测算起。
            started = datetime.fromtimestamp(started_at, tz=self.clock().tzinfo) if started_at > 0 else None
            session = (room_id, started_at if started else None)
            if session != self.auto_session:
                if self.timer_automatic:
                    self.cancel_external_timer()
                self.auto_session = session
                self.auto_session_started = started or self.clock()
                self.auto_handled = None
            if self.external_timer_task is not None and not self.external_timer_task.done():
                return
            if self.auto_handled == session:
                return
            deadline = self.auto_session_started + timedelta(minutes=minutes)
            seconds = max(0, (deadline - self.clock()).total_seconds())
            self.auto_handled = session
            self._set_external_timer(room_id, deadline, seconds / 60 * self.TIMER_SECONDS_PER_MINUTE,
                                     automatic=True)
            self.auto_message = "已检测到直播姬 / OBS 开播，自动计时并在到点后结束B站直播间"
            self.app.push("notice", {"text": f"已自动设置直播间 {room_id} 的停播时间：{deadline:%H:%M:%S}"})
        except asyncio.CancelledError:
            raise
        except Exception:
            self.auto_message = "暂时无法读取直播状态，将自动重试；请检查主播登录与网络"

    def _set_external_timer(self, room_id, deadline, seconds, *, automatic=False):
        self.cancel_external_timer()
        self.timer_room_id = room_id
        self.timer_deadline = deadline
        self.timer_automatic = automatic
        self.external_timer_task = asyncio.create_task(self._external_stop_after(seconds, room_id))

    async def arm_external_timer(self, minutes):
        if type(minutes) is not int or not 1 <= minutes <= 10080:
            raise ValueError("停播倒计时需为 1 至 10080 分钟的整数")
        if self.closing:
            raise ValueError("程序正在退出，无法启动停播倒计时")
        if self.app.broadcast.running:
            raise ValueError("本工具正在推流，请使用开播时的自动停播设置")
        revision, cookie = self.timer_revision, self.app.cfg.get("bili_cookie")
        info = await self.app.broadcast.account()
        if self.closing or revision != self.timer_revision or cookie != self.app.cfg.get("bili_cookie"):
            raise ValueError("停播倒计时设置已取消，请重新设置")
        if not info.get("ok") or not info.get("logged_in") or not info.get("room_id"):
            raise ValueError(info.get("error") or "请先扫码登录正在直播的主播账号")
        if int(info.get("live_status") or 0) != 1:
            raise ValueError("当前登录主播的直播间尚未开播，请先在直播姬开始直播")
        self._set_external_timer(int(info["room_id"]), self.clock() + timedelta(minutes=minutes),
                                 minutes * self.TIMER_SECONDS_PER_MINUTE)
        self.app.push("notice", {"text": f"直播姬停播倒计时已启动，{minutes}分钟后结束直播"})
        return self.status()

    def cancel_external_timer(self):
        self.timer_revision += 1
        task = self.external_timer_task
        if task is not None and task is asyncio.current_task():
            return
        if task and not task.done():
            task.cancel()
        self.external_timer_task = None
        self.timer_deadline = None
        self.timer_room_id = 0
        self.timer_automatic = False

    async def _external_stop_after(self, seconds, room_id):
        try:
            lead = min(seconds, self.FAREWELL_LEAD_SECONDS * self.TIMER_SECONDS_PER_MINUTE / 60)
            await asyncio.sleep(max(0, seconds - lead))
            result = await self.stop_now(farewell=self.farewell_line(), external=True, room_id=room_id,
                                         wait_seconds=lead, stop_on_farewell_error=True)
            message = result.get("error") or result.get("message") or "B站直播间已自动结束"
            self.auto_message = message
            self.last_result = {"action": "stop", "time": self.clock().strftime("%Y-%m-%d %H:%M"),
                                "ok": bool(result.get("ok")), "message": message}
            self.app.push("log", {"level": "info" if result.get("ok") else "warn", "msg": message})
        except asyncio.CancelledError:
            pass
        except Exception:
            self.last_result = {"action": "stop", "time": self.clock().strftime("%Y-%m-%d %H:%M"),
                                "ok": False, "message": "直播姬定时停播失败，请检查主播登录状态与网络"}
            self.app.push("log", {"level": "warn", "msg": self.last_result["message"]})
        finally:
            if self.external_timer_task is asyncio.current_task():
                self.external_timer_task = None
                self.timer_deadline = None
                self.timer_room_id = 0
                self.timer_automatic = False

    async def stop_now(self, *, farewell="", external=False, room_id=0, stop_on_farewell_error=False,
                       wait_seconds=0):
        self.cancel_external_timer()
        self._cancel_local_timer()
        if not farewell:
            self._cancel_daily_farewell()
        self.abort_start = self.stop_requested = True
        now = self.clock()
        self.last_minute = now.strftime("%Y-%m-%d %H:%M")
        if self.settings["enabled"] and now.strftime("%H:%M") == self.settings["start_time"]:
            self.settings["last_start"] = now.date().isoformat()
            config.save(self.app.cfg)
        async with self.execution_lock:
            try:
                if not farewell and not external:
                    return await self.app.broadcast.stop()
                return await self._stop_broadcast(farewell=farewell, external=external, room_id=room_id,
                                                   wait_seconds=wait_seconds,
                                                   stop_on_farewell_error=stop_on_farewell_error)
            finally:
                self.active_action = ""

    async def close(self):
        self.closing = self.abort_start = self.stop_requested = True
        if self._cancel_daily_farewell():
            self.settings["last_stop"] = ""
            config.save(self.app.cfg)
        local_timer = getattr(self.app, "auto_stop_task", None)
        if local_timer:
            local_timer.cancel()
            await asyncio.gather(local_timer, return_exceptions=True)
        if self.auto_check_task:
            self.auto_check_task.cancel()
            await asyncio.gather(self.auto_check_task, return_exceptions=True)
        timer = self.external_timer_task
        self.cancel_external_timer()
        if timer:
            await asyncio.gather(timer, return_exceptions=True)
        if self.loop_task:
            self.loop_task.cancel()
            try:
                await self.loop_task
            except asyncio.CancelledError:
                pass
        if self.jobs:
            await asyncio.gather(*list(self.jobs), return_exceptions=True)
