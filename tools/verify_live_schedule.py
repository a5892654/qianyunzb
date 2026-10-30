# -*- coding: utf-8 -*-
"""千音单窗口每日定时开/停播 · 离线验证：可控 clock + AsyncMock Broadcast + config.save 记录器。
不 new 真实 App、不读真实 config.json、不联网、绝不真实公开开播。运行：".venv\\Scripts\\python.exe" tools\\verify_live_schedule.py"""
import asyncio, json, sys, tempfile, time, traceback, types
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

PROJECT = Path(__file__).resolve().parent.parent
LOG_PATH = Path(r"C:/gamedata/outputs/whaletalk_desktop_build/schedule_tests.log")
sys.path.insert(0, str(PROJECT))
import config

SAVED = []
config.save = lambda cfg: SAVED.append(json.loads(json.dumps(cfg)))
config.load = lambda: {}
from live_schedule import DailyBroadcastSchedule  # noqa: E402

_lines, _results = [], []
def emit(x=""):
    _lines.append(x); print(x, file=sys.stderr, flush=True)
def record(n, st, d=""):
    _results.append((n, st, d)); emit("[%s] %s" % (st, n) + (" :: " + d if d else ""))
def req(c, m):
    if not c:
        raise AssertionError(m)
def dt(iso):
    return datetime.fromisoformat(iso)

def src(h=1111, a=2222, dup=False, miss=False, ok=True, err=""):
    ws = [] if miss else [{"hwnd": h, "title": "DemoTower"}] + (
        [{"hwnd": h + 1, "title": "DemoTower"}] if dup else [])
    return {"ok": ok, "error": err, "windows": ws, "audio": [{"id": a, "name": "loop-x"}]}

def make(iso="2026-10-01 07:00", **st):
    SAVED.clear()
    sch = {"enabled": False, "start_time": "08:00", "stop_time": "18:00", "window_title": "DemoTower",
           "audio_name": "loop-x", "last_start": "", "last_stop": ""}
    sch.update(st)
    br = Mock(running=False)
    br.capture = types.SimpleNamespace(previewing=True, hwnd=1111, audio_device=2222, error="")
    br.selection = Mock(return_value=(1111, 2222))
    br.sources, br.preview = AsyncMock(return_value=src()), AsyncMock(return_value={"ok": True})
    br.stop_preview = AsyncMock(return_value={"ok": True})
    br.start, br.stop = AsyncMock(return_value={"ok": True, "room_id": 42}), AsyncMock(return_value={"ok": True})
    br.account = AsyncMock(return_value={"ok": True, "logged_in": True, "room_id": 42, "live_status": 0})
    app = types.SimpleNamespace(broadcast=br, pushes=[], connect=AsyncMock(),
                                ensure_game_and_dt=AsyncMock(return_value={"ok": True}),
                                cfg={"bili_cookie": "FAKE-SESSDATA", "room_id": 0, "audio": {},
                                     "broadcast": {"title": "千音", "area_id": 591, "schedule": sch}})
    app.push = lambda k, d: app.pushes.append((k, dict(d)))
    now = [dt(iso)]
    return DailyBroadcastSchedule(app, clock=lambda: now[0]), app, now

async def drain(s):
    while s.jobs:
        await asyncio.gather(*list(s.jobs), return_exceptions=True); await asyncio.sleep(0)

async def fire(s, now, iso):
    now[0] = dt(iso); s.tick(); await drain(s)

async def s01_disabled():
    s, a, now = make()
    await fire(s, now, "2026-10-01 08:00")
    req(a.broadcast.start.await_count == 0 and not SAVED, "禁用状态 tick 竟产生动作/写入")
    st = s.status(); req(st["ok"] and not st["enabled"] and "next_start" not in st, "禁用 status 不应含 next_start")

async def s02_day_start_stop():
    s, a, now = make(enabled=True)
    await fire(s, now, "2026-10-01 08:00")
    b, arg = a.broadcast, a.broadcast.start.await_args.args[0]
    req(b.preview.await_args.args[0] == {"hwnd": 1111, "audio_device": 2222}, "未按保存源预览")
    req(arg["confirm"] is True and arg["title"] == "千音" and arg["area_id"] == 591, "start 参数: " + str(arg))
    req(a.cfg["room_id"] == 42 and s.settings["last_start"] == "2026-10-01" and s.last_result["ok"], "开播未落账")
    await asyncio.sleep(0.02); b.running = True
    await fire(s, now, "2026-10-01 18:00")
    req(b.stop.await_count == 1 and s.settings["last_stop"] == "2026-10-01" and s.active_action == "", "停播未落账")

async def s03_cross_midnight():
    s, a, now = make(enabled=True, start_time="23:00", stop_time="02:00")
    await fire(s, now, "2026-10-01 23:00"); a.broadcast.running = True
    await fire(s, now, "2026-10-02 02:00"); a.broadcast.running = False
    await fire(s, now, "2026-10-02 23:00")
    req(a.broadcast.start.await_count == 2 and a.broadcast.stop.await_count == 1
        and s.settings["last_start"] == "2026-10-02" and s.settings["last_stop"] == "2026-10-02",
        "23:00→02:00 跨午夜/次日重复: " + str(s.settings))

async def s04_minute_and_rollback():
    s, a, now = make(enabled=True)
    for iso in ("2026-10-01 08:00", "2026-10-01 08:00", "2026-10-01 07:30", "2026-10-01 08:00"):
        await fire(s, now, iso)
    req(a.broadcast.start.await_count == 1 and a.broadcast.preview.await_count == 1, "同 minute/回拨应 ledger 去重")

async def s05_restart_ledger():
    s, a, now = make(enabled=True, last_start="2026-10-01", last_stop="2026-10-01")
    await fire(s, now, "2026-10-01 08:00"); await fire(s, now, "2026-10-01 18:00")
    req(a.broadcast.start.await_count == 0 and a.broadcast.stop.await_count == 0 and not SAVED,
        "重启后 ledger 应阻止当日重复触发且零写盘")

async def s06_init_save_minute_and_missed():
    s, a, now = make("2026-10-01 08:00", enabled=True)
    s.tick(); await drain(s)
    req(a.broadcast.start.await_count == 0, "初始化落在当前 minute 竟立即开播")
    await fire(s, now, "2026-10-01 09:00")
    req(a.broadcast.start.await_count == 0, "错过的开播分钟竟被补触发")
    now[0] = dt("2026-10-01 18:00")
    await s.save({"enabled": False, "start_time": "08:00", "stop_time": "18:00"})
    s.settings["enabled"] = True  # 仅验证 save 重置 last_minute：同 minute 不得立即触发
    await fire(s, now, "2026-10-01 18:00")
    req(a.broadcast.stop.await_count == 0, "保存落在停播 minute 竟立即停播")

async def s07_skip_and_stop_noop():
    s, a, now = make(enabled=True)
    a.broadcast.running = True
    await fire(s, now, "2026-10-01 08:00")
    req(a.broadcast.preview.await_count == 0 and a.broadcast.start.await_count == 0
        and s.last_result["ok"] and "已在直播" in s.last_result["message"], "自有直播在播应跳过重复开播零平台写")
    s2, a2, now2 = make(enabled=True)
    await fire(s2, now2, "2026-10-01 18:00")
    req(a2.broadcast.stop.await_count == 0 and s2.last_result["ok"]
        and s2.settings["last_stop"] == "2026-10-01", "无直播到停播点不发平台停止请求")

async def s08_sources_bad():
    for name, kw in (("缺失", {"miss": True}), ("重名", {"dup": True})):
        s, a, now = make(enabled=True)
        a.broadcast.sources.return_value = src(**kw)
        await fire(s, now, "2026-10-01 08:00")
        req(a.broadcast.preview.await_count == 0 and a.broadcast.start.await_count == 0
            and s.last_result["ok"] is False, "源" + name + " 竟触达 preview/start")

async def s09_resolve_new_ids():
    s, a, now = make(enabled=True)
    a.broadcast.sources.return_value = src(h=3333, a=4444)
    await fire(s, now, "2026-10-01 08:00")
    arg = a.broadcast.start.await_args.args[0]
    req(a.broadcast.preview.await_args.args[0] == {"hwnd": 3333, "audio_device": 4444}
        and arg["hwnd"] == 3333 and arg["audio_device"] == 4444 and arg["confirm"] is True, "未按保存名称解析新源")

async def s10_fail_once():
    s, a, now = make(enabled=True)
    a.broadcast.preview.return_value = {"ok": False, "error": "灰屏无有效画面"}
    await fire(s, now, "2026-10-01 08:00")
    req(a.broadcast.preview.await_count == 1 and a.broadcast.start.await_count == 0
        and s.last_result["ok"] is False and "灰屏" in s.last_result["message"], "preview 失败应仅一次不重试")
    s2, a2, now2 = make(enabled=True)
    a2.broadcast.start.return_value = {"ok": False, "error": "登录过期"}
    await fire(s2, now2, "2026-10-01 08:00")
    req(a2.broadcast.start.await_count == 1 and s2.last_result["ok"] is False
        and a2.cfg["room_id"] == 0, "start 失败应仅一次且不写 room_id")
    s3, a3, now3 = make(enabled=True)
    a3.broadcast.sources.side_effect = RuntimeError("boom")
    await fire(s3, now3, "2026-10-01 08:00")
    req(s3.last_result["ok"] is False and a3.pushes[-1][1]["level"] == "warn", "执行异常应记 last_result 并 warn")

def payload(**kw):
    d = {"enabled": True, "authorized": True, "title": "千音", "area_id": 591,
         "start_time": "08:00", "stop_time": "18:00"}
    d.update(kw)
    return d

async def s11_save_rejects():
    P, no_src = payload, lambda a: setattr(a.broadcast, "sources", AsyncMock(return_value=src(ok=False, err="读不到")))
    cases = [(P(start_time="24:00"), None, "HH:MM"), (P(stop_time="20:60"), None, "HH:MM"),
             (P(start_time="09:00", stop_time="09:00"), None, "不能相同"),
             ({"enabled": "yes", "start_time": "08:00", "stop_time": "18:00"}, None, "启用"),
             (P(authorized=False), None, "确认"), (P(), lambda a: a.cfg.update(bili_cookie=""), "登录"),
             (P(), lambda a: setattr(a.broadcast.capture, "previewing", False), "预览"),
             (P(), lambda a: setattr(a.broadcast.capture, "hwnd", 999), "预览"),
             (P(), lambda a: setattr(a.broadcast.capture, "error", "黑屏"), "预览"),
             (P(title="  "), None, "标题"), (P(area_id=0), None, "分区"), (P(), no_src, "读不到"),
             (P(), lambda a: setattr(a.broadcast, "sources", AsyncMock(return_value=src(h=5555))), "已变化"),
             (P(), lambda a: setattr(a.broadcast, "sources", AsyncMock(return_value=src(dup=True))), "重名")]
    for data, prep, want in cases:
        s, a, _ = make(enabled=True)
        if prep:
            prep(a)
        n0, err = len(SAVED), None
        try:
            await s.save(data)
        except ValueError as e:
            err = str(e)
        req(err is not None and want in err and len(SAVED) == n0, "save 应拒绝[%s] got=%r" % (want, err))

async def s12_save_enabled_ok():
    s, a, now = make(enabled=True, last_start="2026-09-30")
    st = await s.save(payload(title=" 千音直播 "))
    saved = SAVED[-1]["broadcast"]["schedule"]
    req(st["ok"] and saved["window_title"] == "DemoTower" and saved["audio_name"] == "loop-x"
        and "hwnd" not in saved and "audio_device" not in saved, "只应存名称不存句柄: " + str(saved))
    req(saved["last_start"] == "2026-09-30" and a.cfg["broadcast"]["title"] == "千音直播", "ledger 保留/标题去空格")
    req("next_start" in st and st["local_time"] == "2026-10-01 07:00", "status: " + str(st))

async def s13_disable_keeps_live():
    s, a, now = make(enabled=True)
    a.broadcast.running = True
    await s.save({"enabled": False, "start_time": "08:00", "stop_time": "18:00"})
    req(a.broadcast.stop.await_count == 0 and a.broadcast.running and s.abort_start,
        "关闭定时不得立即停现有直播，只拦后续开播")

async def s14_stop_now_during_slow_preview():
    s, a, now = make(enabled=True)
    ev = asyncio.Event()
    async def slow(_sel):
        await ev.wait()
        return {"ok": True}
    a.broadcast.preview.side_effect = slow
    now[0] = dt("2026-10-01 08:00"); s.tick()
    for _ in range(6):
        await asyncio.sleep(0)
    req(s.active_action == "start" and s.execution_lock.locked(), "execute 应已持有 execution_lock")
    stopper = asyncio.create_task(s.stop_now())
    await asyncio.sleep(0); ev.set()
    await asyncio.gather(*list(s.jobs), stopper)
    req(a.broadcast.start.await_count == 0 and a.broadcast.stop_preview.await_count == 1
        and a.broadcast.stop.await_count == 1 and not s.execution_lock.locked(),
        "慢 preview 期间 stop_now：应取消 start 并串行执行停播")

async def s15_abort_after_start_ok():
    s, a, now = make(enabled=True)
    ev = asyncio.Event()
    async def racing(_data):
        await ev.wait()
        return {"ok": True, "room_id": 42}
    a.broadcast.start.side_effect = racing
    now[0] = dt("2026-10-01 08:00"); s.tick()
    for _ in range(6):
        await asyncio.sleep(0)
    stopper = asyncio.create_task(s.stop_now())
    await asyncio.sleep(0); ev.set()
    await asyncio.gather(*list(s.jobs), stopper); s.tick()
    req(a.broadcast.stop.await_count == 1 and not s.execution_lock.locked()  # stop 恰一次：由 stop_now 串行执行
        and a.cfg["room_id"] == 0 and a.broadcast.start.await_count == 1   # start 成功瞬间停播：不记 room、不再开播
        and "等待停止完成" in s.last_result["message"], "慢 start 期间 stop_now：成功即取消且不漏停播/不双重停")
    req(s.settings["last_start"] == "2026-10-01" and a.broadcast.start.await_count == 1,
        "开播 minute 手动停播后不得同 minute 再开播")

async def s16_close_cancels_pending():
    s, a, now = make(enabled=True)
    ev = asyncio.Event()
    async def slow(_sel):
        await ev.wait()
        return {"ok": True}
    a.broadcast.preview.side_effect = slow
    now[0] = dt("2026-10-01 08:00"); s.tick()
    for _ in range(6):
        await asyncio.sleep(0)
    closer = asyncio.create_task(s.close())
    await asyncio.sleep(0); ev.set(); await closer
    req(s.closing and s.abort_start and s.stop_requested and not s.jobs
        and a.broadcast.start.await_count == 0, "close 应等待 pending job 并取消后续公开动作")

async def s17_sleep_past_stop_cancels_start():
    s, a, now = make(enabled=True)
    ready, release = asyncio.Event(), asyncio.Event()
    async def slow(_selection):
        ready.set(); await release.wait(); return {"ok": True}
    a.broadcast.preview.side_effect = slow
    now[0] = dt("2026-10-01 08:00"); s.tick(); await ready.wait()
    now[0] = dt("2026-10-01 19:00"); release.set(); await drain(s)
    req(a.broadcast.start.await_count == 0 and a.broadcast.stop_preview.await_count == 1,
        "睡眠后已过停播时间，不得补开直播")

async def s18_begin_overrun_stops_once():
    s, a, now = make(enabled=True)
    ready, release = asyncio.Event(), asyncio.Event()
    async def slow(_data):
        ready.set(); await release.wait(); return {"ok": True, "room_id": 42}
    a.broadcast.start.side_effect = slow
    now[0] = dt("2026-10-01 08:00"); s.tick(); await ready.wait()
    now[0] = dt("2026-10-01 19:00"); release.set(); await drain(s)
    req(a.broadcast.stop.await_count == 1 and a.cfg["room_id"] == 0
        and s.settings["last_stop"] == "2026-10-01", "开播完成时已过停播点，必须停止且不宣称开播完成")
    await fire(s, now, "2026-10-01 18:00")
    req(a.broadcast.stop.await_count == 1, "取消开播已执行停播，不得回拨后重复停播")

async def s19_account_change_disables_plan():
    s, a, now = make(enabled=True)
    s.disable_for_account_change()
    await fire(s, now, "2026-10-01 08:00")
    req(not s.settings["enabled"] and a.broadcast.start.await_count == 0
        and "账号" in s.last_result["message"], "账号切换必须关闭计划，避免自动开播到另一账号")

async def phase_api():
    emit("== API. main.py schedule 路由 / /api/config 绕过拒绝（fake config，绝不真实开播）")
    config.LOG_DIR = tempfile.mkdtemp(prefix="wt_sched_logs_")
    try:
        import main as main_mod
    except Exception as exc:
        record("API.import main", "SKIP", repr(exc)); return
    from aiohttp.test_utils import TestClient, TestServer
    s, a, now = make(enabled=True)
    a.schedule = s  # 真实 App.__init__ 同款接线：路由经 app.schedule 访问
    client = TestClient(TestServer(main_mod.build_web(a)))
    hdr = {"X-BVB": "1"}
    await client.start_server()
    try:
        n0 = len(SAVED)
        r = await client.post("/api/config", json={"broadcast": {"schedule": {"start_time": "00:00"}}}, headers=hdr)
        req(r.status == 400 and not (await r.json())["ok"]
            and a.cfg["broadcast"]["schedule"]["start_time"] == "08:00" and len(SAVED) == n0,
            "旧 /api/config 携带 broadcast.schedule 必须 400 且不写入")
        req((await (await client.post("/api/config", json={"audio": {"v": 1}}, headers=hdr)).json())["ok"]
            and a.cfg["audio"]["v"] == 1, "普通 config patch 应仍可用")
        n0 = len(SAVED)
        r = await client.post("/api/broadcast/schedule", json=payload(start_time="08:00"), headers={})
        req(r.status == 403 and len(SAVED) == n0, "POST /broadcast/schedule 缺 X-BVB 应 403 零写入")
        g = await (await client.get("/api/broadcast/schedule")).json()
        req(g["ok"] and g["enabled"] and g["start_time"] == "08:00" and g["next_start"], "GET schedule 契约: " + str(g))
        p = await (await client.post("/api/broadcast/schedule", json=payload(start_time="25:00"), headers=hdr)).json()
        req(not p["ok"] and "HH:MM" in p["error"], "POST schedule 非法时间应拒绝")
        st = await (await client.post("/api/broadcast/stop", headers=hdr)).json()
        req(st["ok"] and a.broadcast.stop.await_count == 1 and s.abort_start and s.stop_requested
            and s.settings["enabled"], "/broadcast/stop 必须走 schedule.stop_now")
    finally:
        await client.close()

async def s20_auto_start_game_and_dt():
    # 到点必须先把游戏/代打拉起来，再找采集源、预览、开播（顺序要看得见）
    s, a, now = make(enabled=True)
    seq = []
    async def note_game(): seq.append("game"); return {"ok": True}
    async def note_preview(_sel): seq.append("preview"); return {"ok": True}
    async def note_start(_data): seq.append("start"); return {"ok": True, "room_id": 42}
    a.ensure_game_and_dt.side_effect, a.broadcast.preview.side_effect = note_game, note_preview
    a.broadcast.start.side_effect = note_start
    await fire(s, now, "2026-10-01 08:00")
    req(seq == ["game", "preview", "start"] and s.last_result["ok"], "定时开播未按 游戏→预览→开播 走: " + str(seq))

    # 游戏起不来 → 一律不碰平台
    s2, a2, now2 = make(enabled=True)
    a2.ensure_game_and_dt.return_value = {"ok": False, "error": "定时开播未执行：游戏启动失败（找不到 Godot）"}
    await fire(s2, now2, "2026-10-01 08:00")
    req(a2.broadcast.preview.await_count == 0 and a2.broadcast.start.await_count == 0
        and s2.last_result["ok"] is False and "游戏启动失败" in s2.last_result["message"],
        "游戏起不来时仍尝试了预览/开播")

    # 刚启动的游戏窗口晚一点才出现：重试到出现为止，并且真的开了播
    s3, a3, now3 = make(enabled=True)
    s3.WINDOW_TRIES = 4
    a3.ensure_game_and_dt.return_value = {"ok": True, "launched": True}
    a3.broadcast.sources.side_effect = [src(miss=True), src(miss=True), src(), src()]
    await fire(s3, now3, "2026-10-01 08:00")
    req(a3.broadcast.sources.await_count == 3 and a3.broadcast.start.await_count == 1
        and s3.last_result["ok"], "刚启动的窗口没有重试等待: " + str(a3.broadcast.sources.await_count))

    # 重试到底仍找不到窗口 → 明确失败，不开播
    s4, a4, now4 = make(enabled=True)
    s4.WINDOW_TRIES = 2
    a4.ensure_game_and_dt.return_value = {"ok": True, "launched": True}
    a4.broadcast.sources.side_effect = lambda: src(miss=True)
    await fire(s4, now4, "2026-10-01 08:00")
    req(a4.broadcast.sources.await_count == 2 and a4.broadcast.start.await_count == 0
        and s4.last_result["ok"] is False and "窗口" in s4.last_result["message"], "窗口始终缺失应失败且不重试到开播")

    # 重试期间被取消：立刻收手，不继续等也不开播
    s5, a5, now5 = make(enabled=True)
    s5.WINDOW_TRIES = 8
    a5.ensure_game_and_dt.return_value = {"ok": True, "launched": True}
    a5.broadcast.sources.side_effect = lambda: src(miss=True)
    async def abort_soon():
        await asyncio.sleep(0.3); s5.abort_start = True
    t = asyncio.create_task(abort_soon())
    await fire(s5, now5, "2026-10-01 08:00"); await t
    req(a5.broadcast.start.await_count == 0 and a5.broadcast.sources.await_count <= 2
        and s5.last_result["ok"] and "取消" in s5.last_result["message"], "取消后仍在等窗口/开播")

    # 本来就在跑（没启动）→ 不该白等十秒
    s6, a6, now6 = make(enabled=True)
    a6.broadcast.sources.side_effect = lambda: src(miss=True)
    await fire(s6, now6, "2026-10-01 08:00")
    req(a6.broadcast.sources.await_count == 1, "游戏没启动却重试等了窗口")


SUITES = [s01_disabled, s02_day_start_stop, s03_cross_midnight, s04_minute_and_rollback, s05_restart_ledger,
          s06_init_save_minute_and_missed, s07_skip_and_stop_noop, s08_sources_bad, s09_resolve_new_ids,
          s10_fail_once, s11_save_rejects, s12_save_enabled_ok, s13_disable_keeps_live,
          s14_stop_now_during_slow_preview, s15_abort_after_start_ok, s16_close_cancels_pending,
          s17_sleep_past_stop_cancels_start, s18_begin_overrun_stops_once, s19_account_change_disables_plan,
          s20_auto_start_game_and_dt]

async def amain():
    t0 = time.time()
    emit("== S. live_schedule.DailyBroadcastSchedule 行为（可控 clock + AsyncMock Broadcast）")
    for fn in SUITES:
        try:
            await asyncio.wait_for(fn(), timeout=15); record(fn.__name__, "PASS")
        except Exception as exc:
            record(fn.__name__, "FAIL", "%s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc(limit=3)))
    try:
        await asyncio.wait_for(phase_api(), timeout=30)
        record("API.路由 6 项（config绕过400/patch可用/缺标头403/GET/非法400/stop_now）", "PASS")
    except Exception as exc:
        record("API.路由", "FAIL" if isinstance(exc, AssertionError) else "SKIP",
               "%s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc(limit=3)))
    p = sum(1 for _, st, _ in _results if st == "PASS")
    f = [n for n, st, _ in _results if st == "FAIL"]
    k = [n for n, st, _ in _results if st == "SKIP"]
    emit(); emit("合计 %d 项：通过 %d，失败 %d，SKIP %d，用时 %.1fs" % (len(_results), p, len(f), len(k), time.time() - t0))
    for n in f:
        emit("  FAIL -> " + n)
    for n in k:
        emit("  SKIP -> " + n)
    return 1 if f or k else 0

def run():
    code = asyncio.run(amain())
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.with_name("schedule_unit_live_schedule.log").write_text("\n".join(_lines) + "\n", encoding="utf-8")
    print("unit log written", file=sys.stderr)
    return code

if __name__ == "__main__":
    sys.exit(run())
