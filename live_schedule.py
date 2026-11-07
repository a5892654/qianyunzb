import asyncio
import re
from datetime import datetime, timedelta

import config
from live_livehime import LivehimeError, parse_hotkey, resolve_path
from netease import music_reference

_CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")

GAME_NAMES = {"demontower": "恶魔之塔 Zero", "stardew": "星露谷物语", "sts2": "杀戮尖塔 2"}


def _minutes(hhmm: str) -> int:
    hour, minute = map(int, hhmm.split(":"))
    return hour * 60 + minute


def _in_window(start: str, stop: str, current: str) -> bool:
    return start <= current < stop if start < stop else current >= start or current < stop


def _segments(start: str, stop: str) -> list[tuple[int, int]]:
    begin, end = _minutes(start), _minutes(stop)
    return [(begin, end)] if begin < end else [(begin, 1440), (0, end)]


def _game_window(game: str, title: str) -> bool:
    text = title.lower()
    if game == "stardew":
        return "stardew" in text or "星露谷" in text
    if game == "sts2":
        return "slay the spire" in text or "杀戮尖塔" in text
    return "demontower" in text or ("godot" in text and "debug" in text)



class DailyBroadcastSchedule:
    # 刚启动的游戏窗口要一两秒才出现在枚举里，重试这么多秒（测试里会调小）
    WINDOW_TRIES = 10
    FAREWELL_TIMEOUT = 90
    FAREWELL_TAIL = 1.2
    FAREWELL_LEAD_SECONDS = 30
    TIMER_SECONDS_PER_MINUTE = 60
    AUTO_CHECK_SECONDS = 15
    STOP_CATCH_UP_SECONDS = 1800
    LIVEHIME_READY_TIMEOUT = 45
    LIVE_STATUS_TRIES = 20
    LIVE_STATUS_INTERVAL = 1.5
    LIVEHIME_STOP_TRIES = 10
    LIVEHIME_STOP_INTERVAL = 1.0
    MUSIC_CHECK_SECONDS = 15

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
        self.active_plan_id = ""
        self.music_check_task = None
        self.music_last_check = None
        self.music_session = None

    @property
    def settings(self):
        return self.app.cfg["broadcast"]["schedule"]

    @property
    def plans(self) -> list[dict]:
        plans = self.settings.get("plans")
        return plans if isinstance(plans, list) else []

    @property
    def plans_active(self) -> bool:
        return bool(self.settings.get("plans_enabled")) and bool(self.plans)

    def within_broadcast_window(self):
        if self.settings.get("mode", "managed") == "stop_only":
            return False
        current = self.clock().strftime("%H:%M")
        if self.plans_active:
            return any(_in_window(plan["start"], plan["stop"], current) for plan in self.plans)
        start, stop = self.settings["start_time"], self.settings["stop_time"]
        return _in_window(start, stop, current)

    async def save(self, data):
        if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
            raise ValueError("请设置是否启用每日直播计划")
        mode = data.get("mode", self.settings.get("mode", "managed"))
        if mode not in ("managed", "stop_only", "livehime"):
            raise ValueError("请选择自动开停播、直播姬自动开停播或直播姬仅定时停播")
        start = data.get("start_time", self.settings["start_time"])
        stop = data.get("stop_time")
        if not isinstance(start, str) or not _CLOCK.fullmatch(start) or not isinstance(stop, str) or not _CLOCK.fullmatch(stop):
            raise ValueError("开播和停播时间必须为 HH:MM 格式")
        if start == stop and mode in ("managed", "livehime"):
            raise ValueError("开播和停播时间不能相同")
        updated = {k: v for k, v in self.settings.items() if k != "livehime"}
        updated.update(enabled=data["enabled"], mode=mode, start_time=start, stop_time=stop,
                       plans_enabled=False)
        livehime = self._livehime_settings(data)
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
        elif data["enabled"] and mode == "livehime":
            self._validate_livehime(livehime)
            info = await self.app.broadcast.account()
            if not info.get("ok") or not info.get("logged_in") or not info.get("room_id"):
                raise ValueError(info.get("error") or "请先扫码登录直播姬使用的主播账号")
            updated["external_room_id"] = int(info["room_id"])
            updated.update(await self._source_names(data))
        elif data["enabled"]:
            updated.update(await self._source_names(data))
        if not data["enabled"] or mode == "stop_only":
            self.abort_start = True
        if self._cancel_daily_farewell():
            updated["last_stop"] = ""
        self.app.cfg["broadcast"].update(schedule=updated, livehime=livehime)
        config.save(self.app.cfg)
        self.last_minute = self.clock().strftime("%Y-%m-%d %H:%M")
        self.last_result = None
        return self.status()

    async def save_plans(self, data):
        """保存播放计划：多段「开始-结束-游戏」，每天重复；启用后优先于单段设置。"""
        if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
            raise ValueError("请设置是否启用播放计划")
        mode = data.get("mode", self.settings.get("mode", "managed"))
        if mode not in ("managed", "stop_only", "livehime"):
            raise ValueError("请选择自动开停播、直播姬自动开停播或直播姬仅定时停播")
        if mode == "stop_only":
            raise ValueError("播放计划包含开播时间，请选择「本工具」或「直播姬」的开停播方式")
        rows = data.get("plans")
        if not isinstance(rows, list):
            raise ValueError("播放计划格式不正确")
        plans = self._clean_plans(rows)
        enabled = data["enabled"]
        if enabled and not plans:
            raise ValueError("请至少添加一段播放计划")
        livehime = self._livehime_settings(data)
        if enabled:
            if data.get("authorized") is not True:
                raise ValueError("启用前需要确认允许每天按时段自动公开开播")
            if not self.app.cfg.get("bili_cookie"):
                raise ValueError("请先扫码登录B站主播账号")
            if mode == "livehime":
                self._validate_livehime(livehime)
            if mode in ("livehime", "stop_only"):
                info = await self.app.broadcast.account()
                if not info.get("ok") or not info.get("logged_in") or not info.get("room_id"):
                    raise ValueError(info.get("error") or "请先扫码登录正在直播的主播账号")
                external_room_id = int(info["room_id"])
        updated = {k: v for k, v in self.settings.items() if k != "livehime"}
        updated.update(enabled=enabled, mode=mode, plans=plans,
                       plans_enabled=bool(enabled and plans))
        if enabled and mode in ("livehime", "stop_only"):
            updated["external_room_id"] = external_room_id
        if not enabled:
            self.abort_start = True
        self.app.cfg["broadcast"].update(schedule=updated, livehime=livehime)
        config.save(self.app.cfg)
        self.last_minute = self.clock().strftime("%Y-%m-%d %H:%M")
        self.last_result = None
        return self.status()

    @staticmethod
    def _clean_plans(rows):
        cleaned, seen = [], set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError("播放计划的每一段必须是时间、游戏组合")
            start, stop = row.get("start"), row.get("stop")
            if (not isinstance(start, str) or not _CLOCK.fullmatch(start)
                    or not isinstance(stop, str) or not _CLOCK.fullmatch(stop)):
                raise ValueError("播放计划的开始和结束时间必须为 HH:MM 格式")
            if start == stop:
                raise ValueError("同一段播放计划的开始和结束时间不能相同")
            game = str(row.get("game") or "")
            if game not in GAME_NAMES:
                raise ValueError("播放计划里的游戏无效")
            playlist, playlist_name = "", ""
            if str(row.get("playlist") or "").strip():
                try:
                    parsed = music_reference(row["playlist"], "playlist")
                except ValueError:
                    raise ValueError("播放计划的开播歌单请填网易云歌单链接或歌单 ID") from None
                if not parsed or parsed[0] != "playlist":
                    raise ValueError("开播歌单需要填写歌单链接，不能填歌曲链接")
                playlist = str(parsed[1])
                playlist_name = str(row.get("playlist_name") or "").strip()[:64]
            plan_id = str(row.get("id") or "").strip()[:64] or f"plan{index + 1}"
            base, suffix = plan_id, 2
            while plan_id in seen:
                plan_id = f"{base}-{suffix}"
                suffix += 1
            seen.add(plan_id)
            cleaned.append({"id": plan_id, "start": start, "stop": stop, "game": game,
                            "playlist": playlist, "playlist_name": playlist_name})
        for index, plan in enumerate(cleaned):
            for other in cleaned[index + 1:]:
                if any(a0 < b1 and b0 < a1
                       for a0, a1 in _segments(plan["start"], plan["stop"])
                       for b0, b1 in _segments(other["start"], other["stop"])):
                    raise ValueError(
                        f"播放计划时段重叠：{plan['start']}–{plan['stop']} 与 "
                        f"{other['start']}–{other['stop']}")
        return cleaned

    def _next_occurrence(self, hhmm, now, ledger=None, key=None):
        hour, minute = map(int, hhmm.split(":"))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        done = (ledger or {}).get(key, "") if key else ""
        if candidate <= now or done == candidate.date().isoformat():
            candidate += timedelta(days=1)
        return candidate

    def _livehime_settings(self, data):
        current = dict(self.app.cfg["broadcast"].get("livehime") or {})
        incoming = data.get("livehime")
        if isinstance(incoming, dict):
            for key in ("path", "hotkey", "click_x", "click_y"):
                if key in incoming:
                    current[key] = incoming[key]
        current["path"] = str(current.get("path") or "").strip()
        current["hotkey"] = str(current.get("hotkey") or "").strip()
        if isinstance(incoming, dict) and "hotkey" in incoming and current["hotkey"]:
            try:
                current["hotkey"] = parse_hotkey(current["hotkey"]).label
            except LivehimeError as error:
                raise ValueError(str(error)) from None
        for key in ("click_x", "click_y"):
            try:
                current[key] = float(current.get(key) or 0)
            except (TypeError, ValueError):
                current[key] = 0.0
        return current

    def _validate_livehime(self, livehime):
        if livehime.get("hotkey"):
            try:
                livehime["hotkey"] = parse_hotkey(livehime["hotkey"]).label
            except LivehimeError as error:
                raise ValueError(str(error)) from None
        x, y = livehime.get("click_x", 0), livehime.get("click_y", 0)
        click_ok = 0.01 <= x <= 0.99 and 0.01 <= y <= 0.99
        if not livehime.get("hotkey") and not click_ok:
            raise ValueError("请先在直播姬「设置 → 快捷键」给开始/停止直播绑定快捷键，并把键名填到定时计划")
        path, source = resolve_path(livehime.get("path"))
        if source == "config_missing":
            raise ValueError("配置的直播姬路径不存在：" + str(livehime.get("path") or ""))
        if not path:
            raise ValueError("没有找到 livehime.exe，请填写直播姬安装目录里的 livehime.exe")
        livehime["path"] = path

    async def _source_names(self, data):
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
        if (sum(w["title"] == window_title for w in sources["windows"]) != 1
                or sum(a["name"] == audio_name for a in sources["audio"]) != 1):
            raise ValueError("窗口或声音设备存在重名，请关闭重复窗口后再保存计划")
        return {"window_title": window_title, "audio_name": audio_name}

    def status(self):
        now = self.clock()
        settings = dict(self.settings)
        plans = self.plans
        plans_active = self.plans_active
        upcoming = {}
        if settings["enabled"] and plans_active:
            starts = [self._next_occurrence(plan["start"], now,
                                            self.settings.get("last_starts") or {}, plan["id"])
                      for plan in plans]
            stops = [self._next_occurrence(plan["stop"], now,
                                           self.settings.get("last_stops") or {}, plan["id"])
                     for plan in plans]
            if starts and settings.get("mode") != "stop_only":
                upcoming["next_start"] = min(starts).strftime("%Y-%m-%d %H:%M")
            if stops:
                upcoming["next_stop"] = min(stops).strftime("%Y-%m-%d %H:%M")
        elif settings["enabled"]:
            for action in (("stop",) if settings.get("mode") == "stop_only" else ("start", "stop")):
                hour, minute = map(int, settings[action + "_time"].split(":"))
                next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_time <= now or settings["last_" + action] == next_time.date().isoformat():
                    next_time += timedelta(days=1)
                upcoming["next_" + action] = next_time.strftime("%Y-%m-%d %H:%M")
        return {"ok": True, **settings, "plans": plans, "plans_enabled": plans_active,
                "livehime": dict(self.app.cfg["broadcast"].get("livehime") or {}),
                **upcoming, "last_result": self.last_result,
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
        self.refresh_music_watch()
        now = self.clock()
        minute = now.strftime("%Y-%m-%d %H:%M")
        # tick 每五秒运行一次，不能只在分钟变化时检查，否则会错过提前告别。
        if not self.closing and self.settings["enabled"]:
            if self.plans_active:
                self._tick_plan_farewell(now)
            else:
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
        if self.plans_active:
            self._fire_plans(now)
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
        # 整分钟和 30 秒告别窗口都可能被长任务/系统休眠错过，停播时间过后 30 分钟内补一次。
        hour, stop_minute = map(int, self.settings["stop_time"].split(":"))
        today_stop = now.replace(hour=hour, minute=stop_minute, second=0, microsecond=0)
        missed = today_stop if today_stop <= now else today_stop - timedelta(days=1)
        late = (now - missed).total_seconds()
        missed_day = missed.date().isoformat()
        if 0 < late <= self.STOP_CATCH_UP_SECONDS and self.settings["last_stop"] != missed_day:
            self.settings["last_stop"] = missed_day
            config.save(self.app.cfg)
            self.abort_start = self.stop_requested = True
            self.app.push("log", {"level": "warn", "msg":
                f"已过定时停播时间（{self.settings['stop_time']}）{int(late // 60)} 分钟，立即补停直播"})
            job = asyncio.create_task(self.execute("stop"))
            self.daily_stop_task = job
            self.jobs.add(job)
            job.add_done_callback(self.jobs.discard)

    def _tick_plan_farewell(self, now):
        stops = dict(self.settings.get("last_stops") or {})
        changed = False
        for plan in self.plans:
            deadline = self._next_occurrence(plan["stop"], now)
            remaining = (deadline - now).total_seconds()
            day = deadline.date().isoformat()
            if 0 < remaining <= self.FAREWELL_LEAD_SECONDS and stops.get(plan["id"]) != day:
                stops[plan["id"]] = day
                changed = True
                self.abort_start = self.stop_requested = True
                job = asyncio.create_task(self.execute("stop", plan=plan, wait_seconds=remaining))
                self.daily_stop_task = job
                self.jobs.add(job)
                job.add_done_callback(self.jobs.discard)
        if changed:
            self.settings["last_stops"] = stops
            config.save(self.app.cfg)

    def _fire_plans(self, now):
        current = now.strftime("%H:%M")
        today = now.date().isoformat()
        starts = dict(self.settings.get("last_starts") or {})
        stops = dict(self.settings.get("last_stops") or {})
        changed = False
        fired = []
        # 先停后开，首尾相接的下一段不会因为上一段的停止标记被拦掉。
        for plan in self.plans:
            if current == plan["stop"] and stops.get(plan["id"]) != today:
                stops[plan["id"]] = today
                changed = True
                self.abort_start = self.stop_requested = True
                fired.append(asyncio.create_task(self.execute("stop", plan=plan)))
        for plan in self.plans:
            if current == plan["start"] and starts.get(plan["id"]) != today:
                starts[plan["id"]] = today
                changed = True
                self.abort_start = self.stop_requested = False
                fired.append(asyncio.create_task(self.execute("start", plan=plan)))
        if changed:
            self.settings["last_starts"] = starts
            self.settings["last_stops"] = stops
            config.save(self.app.cfg)
        for job in fired:
            self.jobs.add(job)
            job.add_done_callback(self.jobs.discard)

    async def execute(self, action, *, plan=None, wait_seconds=0):
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
                    result = await self._stop_broadcast(external=self.settings.get("mode") in ("stop_only", "livehime"),
                                                        room_id=self.settings.get("external_room_id", 0),
                                                        farewell=self.farewell_line(), wait_seconds=wait_seconds,
                                                        stop_on_farewell_error=True)
                    self.active_plan_id = ""
                elif self.app.broadcast.running or self.app.broadcast.phase == "starting":
                    result = {"ok": True, "message": "已在直播，跳过重复开播"}
                else:
                    if plan is not None:
                        await self._switch_game(plan["game"])
                    result = await self.start_broadcast(plan)
                if result.get("ok"):
                    message = result.get("message") or ("定时开播已完成" if action == "start" else "定时停播已完成")
                else:
                    message = result.get("error") or "定时直播操作失败"
                self.last_result = {"action": action, "time": self.clock().strftime("%Y-%m-%d %H:%M"),
                                    "ok": bool(result.get("ok")), "message": message}
                self.app.push("log", {"level": "info" if result.get("ok") else "warn", "msg": message})
                if action == "start" and result.get("ok"):
                    await self._sync_room_title(plan)
                    self._notify_music_started(plan.get("playlist", "") if plan else "")
            except Exception:
                self.last_result = {"action": action, "time": self.clock().strftime("%Y-%m-%d %H:%M"),
                                    "ok": False, "message": "定时直播执行失败，请检查账号与采集源"}
                self.app.push("log", {"level": "warn", "msg": self.last_result["message"]})
            finally:
                self.active_action = ""

    async def _switch_game(self, game: str):
        """播放计划换游戏：先停掉上一款游戏的托管，再切配置并通知启动器。"""
        current = str((self.app.cfg.get("game") or {}).get("active", "demontower"))
        if game == current:
            return False
        if getattr(self.app, "dt", None) is not None and getattr(self.app.dt, "running", False):
            await self.app.stop_dt()
        self.app.cfg.setdefault("game", {})["active"] = game
        config.save(self.app.cfg)
        sync = getattr(getattr(self.app, "game", None), "sync", None)
        if callable(sync):
            try:
                sync(self.app.cfg)
            except Exception:
                pass
        self.app.push("log", {"level": "info", "msg": f"播放计划切换到{GAME_NAMES[game]}"})
        return True

    async def _plan_selection(self, plan):
        """按计划里的游戏找窗口和声音；窗口刚启动时等它出现。"""
        game = plan["game"]
        tries = self.WINDOW_TRIES if plan.get("launched") else 1
        last_error = f"没找到{GAME_NAMES[game]}的游戏窗口"
        for attempt in range(tries):
            data = await self.app.broadcast.sources()
            windows = [w for w in (data.get("windows") or [])
                       if not w.get("minimized") and _game_window(game, str(w.get("title", "")))]
            audio = self._plan_audio(data)
            if data.get("ok") and windows and audio is not None:
                default = int(data.get("default_window") or 0)
                chosen = next((w for w in windows if w.get("hwnd") == default), windows[0])
                return {"hwnd": int(chosen["hwnd"]), "audio_device": audio}
            if not data.get("ok"):
                last_error = data.get("error") or "无法枚举游戏窗口"
            elif not windows:
                last_error = f"没找到{GAME_NAMES[game]}的游戏窗口，请确认游戏已打开且没有最小化"
            else:
                last_error = "没有可用的系统声音输出设备"
            if self.closing or self.abort_start or not self.settings["enabled"] or not self.within_broadcast_window():
                return {"cancelled": True}
            if attempt + 1 < tries:
                await asyncio.sleep(1)
        return {"error": last_error}

    def _plan_audio(self, data):
        audios = data.get("audio") or []
        name = str(self.settings.get("audio_name") or "")
        matches = [a for a in audios if a.get("name") == name] if name else []
        if len(matches) == 1:
            return int(matches[0]["id"])
        default = data.get("default_audio")
        if type(default) is int and any(a.get("id") == default for a in audios):
            return default
        return int(audios[0]["id"]) if audios else None

    async def start_broadcast(self, plan=None):
        prep = await self.app.ensure_game_and_dt()
        if not prep.get("ok"):
            return {"ok": False, "error": prep.get("error") or "定时开播未执行：游戏没能启动"}
        if plan is not None:
            selection = await self._plan_selection({**plan, "launched": bool(prep.get("launched"))})
            if selection.get("cancelled"):
                return {"ok": True, "message": "已取消本次定时开播"}
            if selection.get("error"):
                return {"ok": False, "error": selection["error"]}
        else:
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
            selection = {"hwnd": windows[0]["hwnd"], "audio_device": devices[0]["id"]}
        if self.closing or self.abort_start or not self.settings["enabled"] or not self.within_broadcast_window():
            return {"ok": True, "message": "已取消本次定时开播"}
        if self.settings.get("mode") == "livehime":
            result = await self._start_livehime(selection)
        else:
            preview = await self.app.broadcast.preview(selection)
            if not preview.get("ok"):
                return preview
            if self.closing or self.abort_start or not self.settings["enabled"] or not self.within_broadcast_window():
                await self.app.broadcast.stop_preview()
                return {"ok": True, "message": "已取消本次定时开播"}
            result = await self.app.broadcast.start({**selection, "confirm": True})
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
        if result.get("ok") and plan is not None:
            self.active_plan_id = plan["id"]
            if not result.get("message"):
                result["message"] = f"已按播放计划开播：{GAME_NAMES[plan['game']]}"
        return result

    async def _start_livehime(self, selection):
        info = await self.app.broadcast.account()
        if not info.get("ok") or not info.get("logged_in"):
            return {"ok": False, "error": info.get("error") or "定时开播未执行：无法确认主播登录状态，请重新扫码登录"}
        if not int(info.get("room_id") or 0):
            return {"ok": False, "error": info.get("error") or "定时开播未执行：该账号还没有个人直播间"}
        if int(info.get("live_status") or 0) == 1:
            return await self._adopt_live_room(info, "直播姬已在直播，跳过重复开播")
        if self.closing or self.abort_start or not self.settings["enabled"] or not self.within_broadcast_window():
            return {"ok": True, "message": "已取消本次定时开播"}
        # 源画面先开，直播姬的场景才能采到；它保持开启，下一场直接复用同一窗口。
        source = await self.app.broadcast.start_source(selection)
        if not source.get("ok"):
            return source
        if self.closing or self.abort_start or not self.settings["enabled"] or not self.within_broadcast_window():
            return {"ok": True, "message": "已取消本次定时开播"}
        ready = await self.app.livehime.ensure_running(self.LIVEHIME_READY_TIMEOUT)
        if not ready.get("ok"):
            return ready
        if self.closing or self.abort_start or not self.settings["enabled"] or not self.within_broadcast_window():
            return {"ok": True, "message": "已取消本次定时开播"}
        trigger = await asyncio.to_thread(self.app.livehime.trigger, "start")
        if not trigger.get("ok"):
            return trigger
        self.app.push("log", {"level": "info", "msg": trigger.get("message") or "已通知直播姬开播"})
        live = await self._wait_live_started()
        if live is None:
            return {"ok": False, "error": "已通知直播姬开播，但 B 站 30 秒内没有变成直播中。"
                                          "请检查直播姬已登录同一账号、快捷键绑定为开始/停止直播、"
                                          "场景里有「千音源画面」。源画面已保留，修复后可直接手动重试。"}
        room_id = int(live.get("room_id") or 0)
        if self.closing or self.stop_requested:
            return {"ok": True, "message": "已请求取消本次定时开播，等待停止完成"}
        if self.abort_start or not self.settings["enabled"] or not self.within_broadcast_window():
            stopped = await self._stop_broadcast(external=True, room_id=room_id)
            return {"ok": True, "message": "已取消本次定时开播并停止直播"} if stopped.get("ok") else stopped
        return await self._adopt_live_room(live, f"直播姬已开播，直播间 {room_id}")

    async def _wait_live_started(self):
        for _ in range(self.LIVE_STATUS_TRIES):
            if self.closing:
                return None
            try:
                info = await self.app.broadcast.account()
            except Exception:
                info = {}
            if info.get("ok") and info.get("logged_in") and int(info.get("live_status") or 0) == 1:
                return info
            await asyncio.sleep(self.LIVE_STATUS_INTERVAL)
        return None

    async def _wait_live_stopped(self):
        for _ in range(self.LIVEHIME_STOP_TRIES):
            try:
                info = await self.app.broadcast.account()
            except Exception:
                info = {}
            if info.get("ok") and info.get("logged_in") and int(info.get("live_status") or 0) == 0:
                return True
            await asyncio.sleep(self.LIVEHIME_STOP_INTERVAL)
        return False

    async def _adopt_live_room(self, info, message):
        room_id = int(info.get("room_id") or 0)
        if not room_id:
            return {"ok": False, "error": "直播姬已开播，但读取不到直播间号，请刷新账号后重试"}
        self.app.cfg["room_id"] = room_id
        config.save(self.app.cfg)
        asyncio.create_task(self.app.connect())
        return {"ok": True, "room_id": room_id, "message": message}

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

    async def _stop_autoplay(self):
        if not (getattr(self.app, "dt", None) or getattr(self.app, "dt_task", None)):
            return
        try:
            await self.app.stop_dt()
        except Exception:
            self.app.push("log", {"level": "warn", "msg": "停播时没能暂停代打，请手动点「暂停代打」"})
            return
        self.app.push("log", {"level": "info", "msg": "停播，代打已同步暂停"})

    async def _stop_broadcast(self, *, external=False, room_id=0, farewell="", wait_seconds=0,
                              stop_on_farewell_error=False):
        deadline = asyncio.get_running_loop().time() + wait_seconds
        local = self.app.broadcast.running or self.app.broadcast.phase in ("starting", "stopping")
        live = self.app.broadcast.running
        if not local and (external or self.app.cfg.get("bili_cookie")):
            info = await self.app.broadcast.account()
            if not info.get("ok") or not info.get("logged_in"):
                return {"ok": False, "error": info.get("error") or "无法确认主播直播状态，请重新登录"}
            if external and room_id and int(info.get("room_id") or 0) != room_id:
                return {"ok": False, "error": "主播账号或直播间已变化，请重新设置停播倒计时"}
            room_id = int(info.get("room_id") or 0)
            live = int(info.get("live_status") or 0) == 1 and bool(room_id)
        if farewell and live:
            error = await self._say_farewell(farewell, stop_on_farewell_error)
            if error:
                return error
        # 提前告别，但保留原定停止时刻；合成较慢时优先让最后一句说完。
        await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        if local or live:
            await self._stop_autoplay()
        if local:
            return await self.app.broadcast.stop()
        if live or external:
            if live and self.settings.get("mode") == "livehime":
                # 先按直播姬自己的开关停推流，再用 B 站接口兜底结束直播间。
                triggered = await asyncio.to_thread(self.app.livehime.trigger, "stop")
                if triggered.get("ok"):
                    self.app.push("log", {"level": "info",
                                          "msg": triggered.get("message") or "已通知直播姬停播"})
                    await self._wait_live_stopped()
                else:
                    self.app.push("log", {"level": "warn",
                                          "msg": "直播姬停播快捷键没发出去：" + str(triggered.get("error") or "未知原因")
                                                 + "；继续结束 B站直播间"})
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
                self._notify_music_started()
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

    async def _sync_room_title(self, plan=None):
        sync = getattr(self.app, "sync_room_title", None)
        if callable(sync):
            try:
                await sync(plan.get("game", "") if plan else "")
            except Exception:
                pass

    def _notify_music_started(self, playlist=""):
        notify = getattr(self.app, "notify_live_started", None)
        if callable(notify):
            try:
                notify(playlist)
            except Exception:
                pass

    def _music_watch_enabled(self) -> bool:
        songs = getattr(self.app, "songs", None)
        cfg = getattr(songs, "cfg", None)
        if not isinstance(cfg, dict) or not cfg.get("enabled") or not cfg.get("auto_play_on_live", True):
            return False
        if not str(cfg.get("auto_playlist_ref") or "").strip():
            return False
        broadcast = getattr(self.app, "broadcast", None)
        return bool(getattr(broadcast, "running", False) or getattr(broadcast, "source_open", False))

    def refresh_music_watch(self):
        """自动停播未开启时，也要在直播姬开播后恢复歌单；只查状态，不动停播计时。"""
        if self.closing or not self._music_watch_enabled():
            return
        if getattr(self.app.broadcast, "running", False):
            return  # 本工具推流时开播已经直接触发
        minutes = self.app.cfg["broadcast"].get("auto_stop_minutes", 0)
        if type(minutes) is int and minutes > 0:
            return  # 自动停播监测本来就会查状态，并在发现开播时触发歌单
        if self.music_check_task and not self.music_check_task.done():
            return
        now = self.clock()
        if (self.music_last_check is not None
                and 0 <= (now - self.music_last_check).total_seconds() < self.MUSIC_CHECK_SECONDS):
            return
        self.music_last_check = now
        self.music_check_task = asyncio.create_task(
            self._check_music_live(self.timer_revision, self.app.cfg.get("bili_cookie")))

    async def _check_music_live(self, revision, cookie):
        try:
            info = await self.app.broadcast.account()
            if (self.closing or revision != self.timer_revision
                    or cookie != self.app.cfg.get("bili_cookie")
                    or not self._music_watch_enabled()):
                return
            live = bool(info.get("ok") and info.get("logged_in")
                        and int(info.get("live_status") or 0) == 1)
            if not live:
                self.music_session = None
                return
            session = (int(info.get("room_id") or 0), float(info.get("live_started_at") or 0) or None)
            if session == self.music_session:
                return
            self.music_session = session
            self._notify_music_started()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

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
        external = external or (self.settings.get("mode") == "livehime" and not self.app.broadcast.running)
        self.cancel_external_timer()
        self._cancel_local_timer()
        if not farewell:
            self._cancel_daily_farewell()
        self.abort_start = self.stop_requested = True
        now = self.clock()
        self.last_minute = now.strftime("%Y-%m-%d %H:%M")
        self.active_plan_id = ""
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
        if self.music_check_task:
            self.music_check_task.cancel()
            await asyncio.gather(self.music_check_task, return_exceptions=True)
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
