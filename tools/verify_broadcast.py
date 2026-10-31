# -*- coding: utf-8 -*-
"""千音 B站一键直播 · 离线编排验证（纯 mock，不碰真实 B站 / 真实采集 / 真实游戏）。

运行（必须用项目 venv，主 agent 可复跑）：
    ".venv\\Scripts\\python.exe" tools\\verify_broadcast.py

覆盖：
  A. 真实 live_platform.py 的离线纯函数（推流地址校验 / 错误码转换 / 幂等 end）。
  B. 真实 live_broadcast.py Broadcast 编排（对 mock 的 live_platform / live_capture）。
  C. 真实 main.py 的 /api/broadcast 路由 + POST X-BVB / Host guard（App 用 object.__new__，
     config.save/load 全部换成占位记录器，绝不写真实 config.json）。
  D. web/index.html 直播界面字符串断言（confirm 弹窗、X-BVB 头、无 desktop 回退）。

平台错误 code 转换按当前 live_platform 实现断言；若后续 agent 改动 _friendly/
MANUAL_HINTS，A 组会给出可定位失败。输出同时写 stderr 与 broadcast_tests.log。
"""
from __future__ import annotations

import asyncio
import collections
import copy
import json
import os
import re
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
LOG_PATH = Path(r"C:/gamedata/outputs/whaletalk_desktop_build/broadcast_tests.log")

SECRET_URL = "rtmp://extra.live.fake-domain.test/live/SECRET_PUSH_KEY_9fd21a"
SECRET_MARKER = "SECRET_PUSH_KEY_9fd21a"
FAKE_ROOM_ID = 234567890
PLACEHOLDER_COOKIE = "PLACEHOLDER_SESSDATA_NOT_A_REAL_COOKIE"

sys.path.insert(0, str(PROJECT))

# ---------------------------------------------------------------- 日志 / 结果
_log_lines = []


def emit(line=""):
    _log_lines.append(line)
    print(line, file=sys.stderr, flush=True)


PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results = []


def record(name, status, detail=""):
    _results.append((name, status, detail))
    emit("[{}] {}".format(status, name) + (
        " :: " + detail if detail and status != PASS else ""))


def assert_true(cond, msg):
    if not cond:
        raise AssertionError(msg)


def assert_eq(actual, expected, msg):
    if actual != expected:
        raise AssertionError("{}: 期望 {!r}，实际 {!r}".format(msg, expected, actual))


def assert_no_leak(payload, msg):
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, default=str)
    low = text.lower()
    assert_true(SECRET_MARKER not in text, msg + " 泄露了推流 key")
    assert_true("rtmp" not in low, msg + " 泄露了 rtmp 地址: " + text[:200])
    assert_true(PLACEHOLDER_COOKIE not in text, msg + " 泄露了 Cookie")


def no_write(ctl, msg):
    assert_eq(ctl.begin_calls, [], msg + "（begin 被调用）")
    assert_eq(ctl.end_calls, [], msg + "（end 被调用）")
    assert_eq(ctl.output_starts, [], msg + "（start_output 被调用）")


# ---------------------------------------------------------------- mock 契约层
class PlatformError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self):
        return self.message


class CaptureError(RuntimeError):
    pass


class FaceAuthRequiredError(PlatformError):
    """和 live_platform 里的同名异常同形：带 qr，且仍是 PlatformError 的子类。"""

    def __init__(self, message, qr=""):
        super().__init__(message)
        self.qr = qr


# 和 live_capture 的输出尺寸一致（fake 不做真实采集，只把数字传给上层断言）
OUT_W, OUT_H = 1920, 1080


class Controller:
    """可编程的 mock 后端，记录所有对平台/采集的调用。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.begin_calls = []
        self.end_calls = []
        self.account_calls = 0
        self.areas_calls = 0
        self.preview_calls = []
        self.stop_preview_calls = 0
        self.output_starts = []
        self.stop_output_calls = 0
        self.begin_delay = 0.0
        self.begin_raises = None
        self.end_fails = False
        self.account_result = {"ok": True, "logged_in": True, "name": "假主播",
                               "uid": 1, "room_id": FAKE_ROOM_ID, "live_status": 0}
        self.preview_fail_msg = None
        self.output_fail_msg = None
        self.frame = None
        self.instances = []
        self.captures = []

    @property
    def last_platform(self):
        return self.instances[-1]

    @property
    def last_capture(self):
        return self.captures[-1]


class FakePlatform:
    def __init__(self, cookie):
        self.cookie = cookie
        self.started_room_id = 0
        controller.instances.append(self)

    async def account(self):
        controller.account_calls += 1
        return dict(controller.account_result)

    async def areas(self):
        controller.areas_calls += 1
        return [{"id": 591, "name": "单机游戏", "parent": "游戏"}]

    async def begin(self, title, area_id):
        controller.begin_calls.append((title, area_id))
        if controller.begin_delay:
            await asyncio.sleep(controller.begin_delay)
        if controller.begin_raises is not None:
            raise controller.begin_raises
        self.started_room_id = FAKE_ROOM_ID
        return {"room_id": FAKE_ROOM_ID, "url": SECRET_URL}

    async def end(self):
        controller.end_calls.append(self.started_room_id)
        if not self.started_room_id:
            return {"ok": True, "stopped": False}
        if controller.end_fails:
            raise PlatformError("关闭直播失败，房间号已保留，可稍后重试结束直播")
        self.started_room_id = 0
        return {"ok": True, "stopped": True, "room_id": FAKE_ROOM_ID}


class FakeCapture:
    """契约字段：previewing/output_running/error/hwnd/audio_device + 方法。
       绝不接触任何真实窗口、声音、ffmpeg 或网络。"""

    def __init__(self):
        self.hwnd = 0
        self.audio_device = 0
        self.previewing = False
        self.output_running = False
        self.error = ""
        self.audio_level = 0.0
        controller.captures.append(self)

    def start_preview(self, hwnd, audio_device):
        controller.preview_calls.append((hwnd, audio_device))
        if controller.preview_fail_msg:
            self.error = controller.preview_fail_msg
            raise CaptureError(controller.preview_fail_msg)
        self.hwnd, self.audio_device = int(hwnd), int(audio_device)
        self.previewing = True
        self.error = ""

    def stop_preview(self):
        controller.stop_preview_calls += 1
        self.previewing = False

    def stop_source_frames(self):
        pass

    def start_output(self, target):
        controller.output_starts.append(str(target))
        if controller.output_fail_msg:
            raise CaptureError(controller.output_fail_msg)
        self.output_running = True

    def stop_output(self):
        controller.stop_output_calls += 1
        self.output_running = False

    def snapshot(self):
        return {"previewing": self.previewing, "output_running": self.output_running,
                "audio_level": self.audio_level, "error": self.error,
                "width": OUT_W, "height": OUT_H, "fps": 30}

    def frame_jpeg(self):
        return controller.frame


def sources():
    return {"windows": [{"hwnd": 1111, "title": "DemoTower", "pid": 1,
                         "width": 1280, "height": 720}],
            "audio": [{"id": 2222, "name": "假 loopback"}],
            "default_window": 1111, "default_audio": 2222,
            "ffmpeg_ready": True, "error": ""}


controller = Controller()


def install_fakes():
    """把 mock 的 live_platform / live_capture 塞进 sys.modules，之后
    import live_broadcast / main 只会拿到 mock，不加载真实采集。"""
    mod_p = types.ModuleType("live_platform")
    mod_p.PlatformError = PlatformError
    mod_p.FaceAuthRequiredError = FaceAuthRequiredError
    mod_p.BiliBroadcastPlatform = FakePlatform
    mod_c = types.ModuleType("live_capture")
    mod_c.CaptureError = CaptureError
    mod_c.WindowStreamCapture = FakeCapture
    mod_c.sources = sources
    mod_c.OUT_W, mod_c.OUT_H = OUT_W, OUT_H   # live_broadcast 从这里取源画面尺寸
    mod_c.fit_borderless_client = lambda hwnd, w, h: (w, h)
    mod_c.close_window = lambda hwnd: None
    sys.modules["live_platform"] = mod_p
    sys.modules["live_capture"] = mod_c


# ---------------------------------------------------------------- A. 真实 live_platform 纯函数
async def phase_a():
    emit("== A. live_platform 离线安全检查（真实模块，零网络）")
    try:
        import live_platform as real
    except Exception as exc:
        record("A.import live_platform", SKIP, "导入失败，待主 agent 环境验证: " + str(exc))
        return
    record("A.import live_platform", PASS)

    vp = real._validate_push_url
    bad = {
        "回环主机": ("rtmp://127.0.0.1/live/", "k"),
        "IP字面量": ("rtmp://10.1.2.3/live/", "k"),
        "IPv6字面量": ("rtmp://[::1]/live/", "k"),
        "localhost": ("rtmp://localhost/live/", "k"),
        "内嵌账号": ("rtmp://user:pass@host.example.com/live/", "k"),
        "控制字符": ("rtmp://host.example.com/live/\nk", "k"),
        "空地址": ("", "k"),
        "空key": ("rtmp://host.example.com/live/", ""),
        "错误协议": ("http://host.example.com/live/", "k"),
    }
    try:
        assert_eq(vp("rtmp://other.live.bilibili.com/live/", "abc123"),
                  "rtmp://other.live.bilibili.com/live/abc123", "合法官方地址拼接")
        assert_eq(vp("rtmps://push.bilibili.com/live", "key/9"),
                  "rtmps://push.bilibili.com/live/key/9", "合法 rtmps 拼接")
        record("A._validate_push_url 接受合法官方地址", PASS)
    except Exception as exc:
        record("A._validate_push_url 接受合法官方地址", FAIL, str(exc))
    for name, (addr, key) in bad.items():
        try:
            vp(addr, key)
            record("A._validate_push_url 拒绝" + name, FAIL, "未抛 PlatformError")
        except real.PlatformError as pe:
            ok = "rtmp://" not in str(pe)
            record("A._validate_push_url 拒绝" + name, PASS if ok else FAIL,
                  "" if ok else "错误文案回显了地址")
        except Exception as exc:
            record("A._validate_push_url 拒绝" + name, FAIL, "异常类型错误: " + repr(exc))

    friendly = real._friendly
    try:
        from bilibili_api.exceptions import NetworkException, ResponseCodeException
        code_cls_ok = True
    except Exception:
        code_cls_ok = False
    if code_cls_ok:
        def make_resp(code):
            for args in ((code, {"raw": "secret-data"}), (code,)):
                try:
                    return ResponseCodeException(*args)
                except Exception:
                    continue
            return None

        mapped = 0
        for code, hint_key in ((-352, "风控"), (60013, "实名"), (60034, "直播姬"), (65530, "过期")):
            exc = make_resp(code)
            if exc is None:
                record("A._friendly code=" + str(code), SKIP,
                       "无法构造 ResponseCodeException，待主 agent 补验")
                continue
            pe = friendly(exc)
            try:
                assert_true(isinstance(pe, real.PlatformError), "未返回 PlatformError")
                assert_true(hint_key in str(pe), "缺少人工提示（期望含 " + hint_key + "）: " + str(pe))
                assert_true("secret-data" not in str(pe), "错误文案携带原始响应")
                assert_true("rtmp" not in str(pe).lower(), "错误文案含 rtmp")
                assert_eq(pe.code, code, "错误码保留")
                mapped += 1
            except AssertionError as ae:
                record("A._friendly code=" + str(code), FAIL, str(ae))
        if mapped:
            record("A._friendly 已知风控/资格错误码→安全文案 (" + str(mapped) + " 例)", PASS)
        exc = make_resp(999999)
        if exc is not None:
            pe = friendly(exc)
            ok = ("999999" in str(pe) or "code=" in str(pe)) and "secret-data" not in str(pe)
            record("A._friendly 未知 code→通用文案不带原始数据", PASS if ok else FAIL, str(pe))
        try:
            pe = friendly(ResponseCodeException(60024, "本次开播需要完成人脸验证"))
            ok = ("人脸验证" in str(pe)) and ("本次开播需要完成人脸验证" in str(pe)) and pe.code == 60024
            record("A._friendly 60024→人工提示并附 B站原话", PASS if ok else FAIL, str(pe))
            # 短到不会被截断救场：必须靠过滤规则本身挡下推流信息
            leaked = str(friendly(ResponseCodeException(60024, "见 rtmp://a KEY=SECRET")))
            assert_true("rtmp" not in leaked.lower() and "SECRET" not in leaked and "见" not in leaked,
                        "推流信息被回显: " + leaked[:120])
            dirty = str(friendly(ResponseCodeException(60013, "带\x00控制符\t" + "啊" * 300)))
            assert_true("\x00" not in dirty and "\t" not in dirty and len(dirty) < 200,
                        "B站原话没被清洗/截断: " + dirty[:120])
            record("A._friendly B站原话过滤推流信息并清洗截断", PASS)
        except Exception as exc3:
            record("A._friendly 60024 原话回显", FAIL if isinstance(exc3, AssertionError) else SKIP, repr(exc3))
        try:
            net = NetworkException(412, "Forbidden")
            pe = friendly(net)
            ok = "412" in str(pe) or "验证" in str(pe)
            record("A._friendly 网络412→人工验证提示", PASS if ok else FAIL, str(pe))
        except Exception as exc2:
            record("A._friendly NetworkException(412)", SKIP,
                   "无法构造 NetworkException，待主 agent 补验: " + repr(exc2))
    else:
        record("A._friendly 错误码转换", SKIP, "bilibili_api.exceptions 不可导入，待主 agent 补验")

    pe_timeout = friendly(asyncio.TimeoutError())
    ok = "超时" in str(pe_timeout)
    record("A._friendly 超时映射", PASS if ok else FAIL, str(pe_timeout))
    passthrough = real.PlatformError("原样", code=7)
    ok = friendly(passthrough) is passthrough
    record("A._friendly PlatformError 透传", PASS if ok else FAIL, "被吞掉了原始错误")

    async def a_idempotent():
        inst = real.BiliBroadcastPlatform("")
        assert_eq(inst.started_room_id, 0, "新实例 started_room_id 应为 0")
        res = await inst.end()
        assert_eq(res.get("stopped"), False, "未开播 end 应幂等 stopped=False")
        assert_true(res.get("ok"), "未开播 end 应 ok=True")
        acct = await inst.account()
        assert_eq(acct.get("logged_in"), False, "空 Cookie 应 logged_in=False")
        assert_true(acct.get("ok") is True, "account 应 ok=True")
        assert_no_leak(acct, "真实 account 空 Cookie 返回")
    try:
        await a_idempotent()
        record("A.空Cookie account 不联网 / end 幂等 / 无凭证泄露", PASS)
    except Exception as exc:
        record("A.空Cookie account 不联网 / end 幂等 / 无凭证泄露", FAIL, str(exc))


# ---------------------------------------------------------------- B. Broadcast 编排（真实 live_broadcast）
# 注意：install_fakes() 必须在 phase_a() 取完真实 live_platform 之后才执行，
# 否则会遮住真实模块。setup_orchestration() 负责这个顺序。
Broadcast = None
running_apps = []


def setup_orchestration():
    global Broadcast
    if Broadcast is None:
        install_fakes()
        import live_broadcast  # noqa: E402  （mock live_platform/live_capture 就位后导入）
        Broadcast = live_broadcast.Broadcast
    return Broadcast


def make_app():
    app = types.SimpleNamespace(
        cfg={"bili_cookie": PLACEHOLDER_COOKIE, "room_id": 0},
        pushes=[],
        bili=types.SimpleNamespace(login_uid=None, login_name=""),
        connect_calls=0,
    )

    def push(kind, data):
        app.pushes.append((kind, copy.deepcopy(data)))

    app.push = push

    async def _connect():
        app.connect_calls += 1

    app.connect = _connect
    running_apps.append(app)
    return app


def fresh_broadcast():
    app = make_app()
    return app, Broadcast(app)


def sel(hwnd=1111, audio=2222, **over):
    d = {"hwnd": hwnd, "audio_device": audio}
    d.update(over)
    return d


START_ARGS = {"title": "千音直播测试", "area_id": 591, "confirm": True}


async def ok_preview(b, hwnd=1111, audio=2222):
    r = await b.preview(sel(hwnd=hwnd, audio=audio))
    assert_true(r.get("ok"), "预览应成功: " + str(r))


async def go_live(app, b):
    await ok_preview(b)
    r = await b.start({**sel(), **START_ARGS})
    assert_true(r.get("ok"), "开播应成功: " + str(r))
    await asyncio.sleep(0.05)  # 让 _watch 任务真正跑起来（模拟真实 HTTP 间隔）
    return r


async def t_b01_no_confirm():
    controller.reset()
    app, b = fresh_broadcast()
    await ok_preview(b)
    r = await b.start({**sel(), **START_ARGS, "confirm": False})
    assert_true(not r.get("ok"), "confirm=False 竟成功开播")
    assert_true("确认" in r.get("error", ""), "错误应提示确认: " + str(r))
    args = {**sel(), "title": "t", "area_id": 591}
    r2 = await b.start(args)
    assert_true(not r2.get("ok"), "缺 confirm 字段竟成功开播")
    no_write(controller, "无确认不得触达平台/输出")
    assert_eq(b.phase, "error", "失败后 phase")
    record("B01 无 confirm 拒绝开播且零平台写", PASS)


async def t_b02_bad_selection():
    controller.reset()
    app, b = fresh_broadcast()
    bad = [
        (dict(START_ARGS), "缺字段"),
        ("not-a-dict", "非 dict"),
        (sel(hwnd=True), "hwnd 是 bool"),
        (sel(hwnd=0), "hwnd=0"),
        (sel(hwnd=-5), "hwnd 负数"),
        (sel(hwnd="123"), "hwnd 字符串"),
        (sel(audio=True), "audio 是 bool"),
        (sel(audio=-1), "audio 负数"),
        (sel(audio="2222"), "audio 字符串"),
    ]
    for data, name in bad:
        r = await b.start(data)
        r2 = await b.preview(data)
        assert_true(not r.get("ok"), name + " 竟被 start 接受: " + str(r))
        assert_true(not r2.get("ok"), name + " 竟被 preview 接受: " + str(r2))
    no_write(controller, "错参数不得触达平台/输出")
    assert_eq(controller.preview_calls, [], "错参数不得触达 capture 预览")
    record("B02 hwnd/audio 非法类型与0值全部拒绝（含 bool）", PASS)


async def t_b03_bad_title_area():
    controller.reset()
    app, b = fresh_broadcast()
    await ok_preview(b)
    bad_titles = [{"title": "", "area_id": 591}, {"title": "   ", "area_id": 591},
                  {"title": "x" * 61, "area_id": 591}, {"title": 123, "area_id": 591}]
    for over in bad_titles:
        r = await b.start({**sel(), **START_ARGS, **over})
        assert_true(not r.get("ok"), "坏标题被接受 " + str(over) + ": " + str(r))
    bad_areas = [{"area_id": 0}, {"area_id": -3}, {"area_id": "591"},
                 {"area_id": True}, {"area_id": None}]
    for over in bad_areas:
        r = await b.start({**sel(), **START_ARGS, **over})
        assert_true(not r.get("ok"), "坏分区被接受 " + str(over) + ": " + str(r))
    no_write(controller, "标题/分区非法不得触达平台")
    record("B03 标题(空/超长/非str)与分区(0/负/字符串/bool)拒绝", PASS)


async def t_b04_no_preview():
    controller.reset()
    app, b = fresh_broadcast()
    r = await b.start({**sel(), **START_ARGS})
    assert_true(not r.get("ok"), "未预览竟开播")
    assert_true("预览" in r.get("error", ""), "应提示先预览: " + str(r))
    no_write(controller, "未预览不得触达平台")
    record("B04 无预览拒绝开播且零平台写", PASS)


async def t_b05_preview_mismatch():
    controller.reset()
    app, b = fresh_broadcast()
    await ok_preview(b, hwnd=1111, audio=2222)
    r = await b.start({**sel(hwnd=9999, audio=2222), **START_ARGS})
    assert_true(not r.get("ok"), "换窗口竟开播")
    r2 = await b.start({**sel(hwnd=1111, audio=8888), **START_ARGS})
    assert_true(not r2.get("ok"), "换音频设备竟开播")
    no_write(controller, "预览源不一致不得触达平台")
    record("B05 preview 与 start 源不一致拒绝", PASS)


async def t_b06_black_screen():
    controller.reset()
    app, b = fresh_broadcast()
    controller.preview_fail_msg = "采集无有效画面（窗口黑屏或已关闭），已回退"
    r = await b.preview(sel())
    assert_true(not r.get("ok") and "黑屏" in r.get("error", ""), "黑屏应回退: " + str(r))
    assert_eq(b.phase, "error", "黑屏后 phase")
    r2 = await b.start({**sel(), **START_ARGS})
    assert_true(not r2.get("ok"), "黑屏后竟开播")
    no_write(controller, "黑屏不得触达平台")
    # 预览成功后采集报错（运行期分类错误）→ start 仍须拒绝
    controller.preview_fail_msg = None
    await ok_preview(b)
    controller.last_capture.error = "推流连接中断"
    r3 = await b.start({**sel(), **START_ARGS})
    assert_true(not r3.get("ok"), "capture.error 置位竟开播")
    no_write(controller, "预览报错不得触达平台")
    record("B06 预览黑屏/预览报错 → start 拒绝零平台写", PASS)


async def t_b07_live_happy_and_no_leak():
    controller.reset()
    app, b = fresh_broadcast()
    r = await go_live(app, b)
    assert_eq(r.get("room_id"), FAKE_ROOM_ID, "start 返回 room_id")
    assert_true(not any(k in r for k in ("url", "rtmp", "key")),
                "start 返回多余字段: " + str(list(r)))
    assert_no_leak(r, "start 成功返回")
    assert_eq(controller.begin_calls, [("千音直播测试", 591)], "begin 恰好一次")
    assert_eq(controller.output_starts, [SECRET_URL], "url 仅内部传给 capture")
    st = b.status()
    assert_eq(st["phase"], "live", "capture ready 后 status 应 live")
    assert_true(st["running"], "running 标志")
    assert_no_leak(st, "status")
    acc = await b.account()
    assert_no_leak(acc, "account")
    assert_no_leak(json.dumps(app.pushes, ensure_ascii=False), "push 日志")
    await b.stop()
    await b.close()
    record("B07 正常开播→live、url仅内部、返回零泄露", PASS)


async def t_b08_concurrent_start():
    controller.reset()
    app, b = fresh_broadcast()
    await ok_preview(b)
    controller.begin_delay = 0.15
    r1, r2 = await asyncio.gather(b.start({**sel(), **START_ARGS}),
                                  b.start({**sel(), **START_ARGS}))
    oks = [r for r in (r1, r2) if r.get("ok")]
    assert_true(len(oks) == 1, "并发开播只应有1个成功: " + str(r1) + " " + str(r2))
    assert_eq(len(controller.begin_calls), 1, "begin 只应调用一次（靠锁）")
    r3 = await b.start({**sel(), **START_ARGS})
    assert_true(not r3.get("ok") and "直播" in r3.get("error", ""),
                "重复开播应拒绝: " + str(r3))
    assert_eq(len(controller.begin_calls), 1, "重复开播后又多 begin")
    await b.stop()
    record("B08 重复/并发 start 仅一次 begin（靠 lock）", PASS)


async def t_b09_other_stream_never_end():
    controller.reset()
    app, b = fresh_broadcast()
    await ok_preview(b)
    controller.begin_raises = PlatformError(
        "检测到该账号已有正在进行的直播，本工具不会接管或停止已有推流，请先人工结束当前直播", 0)
    r = await b.start({**sel(), **START_ARGS})
    assert_true(not r.get("ok"), "他程序已开播时竟成功")
    assert_true("已有" in r.get("error", "") or "人工" in r.get("error", ""),
                "应提示人工处理: " + str(r))
    assert_eq(controller.end_calls, [], "绝不允许 end 别人的直播")
    assert_eq(controller.last_platform.started_room_id, 0, "started_room_id 不应被置位")
    assert_true(not b.running, "失败后不应自认为 running")
    record("B09 其他程序已开播→begin拒绝且从不调用end", PASS)


async def t_b10_encoder_fail():
    controller.reset()
    app, b = fresh_broadcast()
    await ok_preview(b)
    controller.output_fail_msg = "硬件编码器 h264_nvenc 初始化失败"
    r = await b.start({**sel(), **START_ARGS})
    assert_true(not r.get("ok"), "编码器失败竟开播成功")
    assert_eq(len(controller.begin_calls), 1, "begin 一次")
    assert_eq(controller.end_calls, [FAKE_ROOM_ID], "失败回滚必须 end 仅自己开过的房间")
    assert_true(controller.stop_output_calls >= 1, "应尝试 stop_output")
    assert_eq(b.phase, "error", "phase")
    assert_no_leak(r, "encoder 失败返回")
    record("B10 编码器启动失败→end 仅 self.started_room_id", PASS)


async def t_b11_stop_keeps_game_and_tts():
    controller.reset()
    hit = {"dt": 0, "engine": 0, "player": 0}
    app = make_app()
    app.dt = types.SimpleNamespace(running=True, stop=lambda: hit.__setitem__("dt", hit["dt"] + 1))
    app.engine = types.SimpleNamespace(proc=None, stop=lambda *a, **k: hit.__setitem__("engine", hit["engine"] + 1))
    app.player = types.SimpleNamespace(close=lambda: hit.__setitem__("player", hit["player"] + 1))
    b = Broadcast(app)
    await go_live(app, b)
    r = await b.stop()
    assert_true(r.get("ok"), "停播应成功: " + str(r))
    assert_eq(len(controller.output_starts), 1, "输出启动一次")
    assert_true(controller.stop_output_calls >= 1, "stop 应停自有 capture 输出")
    assert_eq(controller.end_calls, [FAKE_ROOM_ID], "stop 应 end 自己房间")
    assert_eq(hit, {"dt": 0, "engine": 0, "player": 0}, "停直播不得动代打/TTS/播放器")
    assert_eq(b.status()["phase"], "preview", "停播后回到 preview（预览保留）")
    record("B11 stop 正常停自有capture与end、不停代打/TTS", PASS)


async def t_b12_stop_fail_retry():
    controller.reset()
    app, b = fresh_broadcast()
    await go_live(app, b)
    controller.end_fails = True
    r = await b.stop()
    assert_true(not r.get("ok"), "end 失败应报错")
    assert_eq(controller.last_platform.started_room_id, FAKE_ROOM_ID,
              "end 失败必须保留 owned 房间")
    assert_no_leak(r, "stop 失败返回")
    controller.end_fails = False
    r2 = await b.stop()
    assert_true(r2.get("ok"), "重试 stop 应成功: " + str(r2))
    assert_eq(controller.last_platform.started_room_id, 0, "重试成功后应清掉 owned")
    assert_eq(len(controller.end_calls), 2, "两次 end 调用")
    record("B12 stop 失败保留 owned 可重试成功", PASS)


async def t_b13_watch_stops_own_live():
    controller.reset()
    app, b = fresh_broadcast()
    await go_live(app, b)
    controller.last_capture.error = "窗口采集已关闭（最小化或退出），停止输出"
    for _ in range(40):
        await asyncio.sleep(0.1)
        if not b.running:
            break
    assert_true(not b.running, "监控 capture 关闭后应自动停止 own 直播")
    assert_eq(controller.end_calls, [FAKE_ROOM_ID], "自动停止应 end 自己房间")
    assert_eq(b.status()["phase"], "error", "自动停止后 phase=error")
    record("B13 监控到 capture 关闭自动停止自有直播", PASS)


async def t_b14_start_stop_race():
    controller.reset()
    app, b = fresh_broadcast()
    await ok_preview(b)
    controller.begin_delay = 0.3
    t1 = asyncio.create_task(b.start({**sel(), **START_ARGS}))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(b.stop())
    r1, r2 = await asyncio.gather(t1, t2)
    assert_true(r1.get("ok"), "start 应成功: " + str(r1))
    assert_true(r2.get("ok"), "stop 应成功（锁串行化）: " + str(r2))
    assert_eq(len(controller.begin_calls), 1, "begin 一次")
    assert_eq(controller.end_calls, [FAKE_ROOM_ID], "end 一次且只关自己")
    assert_true(not b.running, "race 后不得残留 running 状态")
    assert_eq(controller.last_capture.output_running, False, "race 后输出应停")
    await asyncio.sleep(0.05)
    assert_true(b.watch_task is None or b.watch_task.done(), "watch 任务应结束")
    record("B14 start/stop 竞态经锁串行、状态自洽", PASS)


async def t_b15_preview_no_login():
    controller.reset()
    app, b = fresh_broadcast()
    controller.account_result = {"ok": True, "logged_in": False, "name": "",
                                 "uid": 0, "room_id": 0, "live_status": 0}
    await ok_preview(b)
    assert_eq(controller.begin_calls, [], "窗口预览不得触发 begin")
    assert_eq(controller.account_calls, 0, "preview 不应查询账号（不需登录）")
    assert_eq(controller.preview_calls, [(1111, 2222)], "preview 落到 capture")
    assert_eq(b.status()["phase"], "preview", "预览 phase")
    r = await b.start({**sel(), **START_ARGS})
    assert_true(r.get("ok"), "预览后开播仍应成功（登录检查属于平台层）")
    await asyncio.sleep(0.05)  # 等 watch 任务启动，避免撞上 B17 记录的边界缺陷
    await b.stop()
    record("B15 窗口预览无需登录且不 begin", PASS)


async def t_b16_preview_locked_while_live():
    controller.reset()
    app, b = fresh_broadcast()
    await go_live(app, b)
    r = await b.preview(sel(hwnd=1111, audio=8888))
    assert_true(not r.get("ok"), "直播中竟能切换采集源")
    r2 = await b.stop_preview()
    assert_true(not r2.get("ok"), "直播中竟能关预览")
    assert_eq(len(controller.preview_calls), 1, "直播中不得再 start_preview")
    assert_eq(controller.stop_preview_calls, 0, "直播中不得 stop_preview")
    await b.stop()
    record("B16 直播期间禁止切换/关闭采集源", PASS)


async def t_b17_stop_immediately_after_start():
    """产品缺陷探针：start 返回后未经事件循环切换立即 stop/close。
    生产里对应“刚点开播就退出程序”，shutdown 走 close() 同路径。"""
    controller.reset()
    app, b = fresh_broadcast()
    await ok_preview(b)
    r = await b.start({**sel(), **START_ARGS})
    assert_true(r.get("ok"), "开播应成功: " + str(r))
    defect = None
    try:
        await b.stop()  # watch_task 尚未开始运行：cancel+await 会把 CancelledError 抛给调用方
    except asyncio.CancelledError:
        defect = "stop() 向调用方抛出 CancelledError（watch_task 未先运行即被 cancel+await）"
    if defect:
        # 缺陷现场：stop 半途崩溃，输出没停、房间没 end、phase 卡在 stopping
        assert_true(controller.last_platform.started_room_id != 0, "缺陷场景下应残留 owned 房间")
        assert_eq(controller.end_calls, [], "崩溃路径未 end 自己刚开的房间（直播会一直开着）")
        assert_true(controller.last_capture.output_running, "崩溃路径未 stop_output")
        assert_eq(b.phase, "stopping", "phase 应卡在 stopping")
        await b.platform.end()  # 手工清场，避免污染后续测试
        record("B17 start 后立即 stop/close（同任务未经事件循环切换）抛 CancelledError、不关自己房间",
               FAIL, defect + " — 建议 stop() 中 `await self.watch_task` 包 try/except CancelledError；"
                             "复现：preview→start 后同一任务立即 await stop()，无中间 await")
    else:
        record("B17 start 后立即 stop 正常收口", PASS)


async def t_b18_preview_reuses_same_session():
    """重复点「预览」不该停掉正在用的采集会话：生产实测 stop 后立刻 start 会把整个
    服务崩掉（WGC 拆会话时访问违例），而用户什么都没换，本来就不该重来一遍。"""
    controller.reset()
    app, b = fresh_broadcast()
    await ok_preview(b)
    r = await b.preview(sel())
    assert_true(r.get("ok"), "同参数重复预览应成功: " + str(r))
    assert_eq(controller.stop_preview_calls, 0, "同参数重复预览不该停掉正在用的采集会话")
    assert_eq(controller.preview_calls, [(1111, 2222)], "同参数重复预览不该重开采集")
    r2 = await b.preview(sel(hwnd=9999))
    assert_true(r2.get("ok"), "换窗口应正常切换: " + str(r2))
    assert_eq(controller.stop_preview_calls, 1, "换窗口要先停旧会话")
    assert_eq(len(controller.preview_calls), 2, "换窗口要重开采集")
    record("B18 同参数重复预览复用采集会话，换窗口才重启", PASS)


BROADCAST_TESTS = [
    t_b01_no_confirm, t_b02_bad_selection, t_b03_bad_title_area, t_b04_no_preview,
    t_b05_preview_mismatch, t_b06_black_screen, t_b07_live_happy_and_no_leak,
    t_b08_concurrent_start, t_b09_other_stream_never_end, t_b10_encoder_fail,
    t_b11_stop_keeps_game_and_tts, t_b12_stop_fail_retry, t_b13_watch_stops_own_live,
    t_b14_start_stop_race, t_b15_preview_no_login, t_b16_preview_locked_while_live,
    t_b17_stop_immediately_after_start, t_b18_preview_reuses_same_session,
]


async def run_broadcast_tests():
    emit("== B. live_broadcast.Broadcast 真实编排 × mock 平台/采集")
    try:
        setup_orchestration()
    except Exception as exc:
        record("B.setup 导入真实 live_broadcast(mock依赖)", FAIL,
               repr(exc) + "\n" + traceback.format_exc(limit=4))
        return
    for fn in BROADCAST_TESTS:
        name = fn.__name__
        try:
            await asyncio.wait_for(fn(), timeout=15)
        except (Exception, asyncio.CancelledError) as exc:
            record(name, FAIL, "{}: {}\n{}".format(
                type(exc).__name__, exc, traceback.format_exc(limit=4)))
        controller.reset()


# ---------------------------------------------------------------- C. HTTP 路由
async def phase_c():
    emit("== C. main.py /api/broadcast 路由（X-BVB/Host guard、frame、账号互斥）")
    import config
    tmp_log = tempfile.mkdtemp(prefix="wt_verify_logs_")
    config.LOG_DIR = tmp_log  # main import 的 app.log 落此处，不碰项目 logs/
    saved_calls = []
    orig_save, orig_load = config.save, config.load
    config.save = lambda cfg: saved_calls.append(copy.deepcopy(dict(cfg)))
    config.load = lambda: {"bili_cookie": PLACEHOLDER_COOKIE, "room_id": 0}
    try:
        import main as main_mod
    except Exception as exc:
        record("C.import main", SKIP,
               "真实 main 不可导入（待主 agent 环境）: " + repr(exc) + "\n" +
               traceback.format_exc(limit=3))
        config.save, config.load = orig_save, orig_load
        return
    record("C.import main（fake live_* 就位、config 已换占位）", PASS)

    # 假 App：object.__new__ 绕过真实 __init__（不 config.load、不起任何引擎）
    app = object.__new__(main_mod.App)
    app.cfg = {"bili_cookie": PLACEHOLDER_COOKIE, "room_id": 0, "songs": {},
               "tts": {}, "ai": {}, "audio": {},
               # /api/broadcast/stop 走 schedule.stop_now，需真实 settings 结构（禁用态，不触发写盘）
               "broadcast": {"title": "", "area_id": 0, "schedule": {"enabled": False, "start_time": "08:00",
                               "stop_time": "18:00", "window_title": "", "audio_name": "",
                               "last_start": "", "last_stop": ""}}}
    app.ws = set()
    app.history = collections.deque(maxlen=300)
    app.status = {}
    app.pushes = []
    app.push = lambda kind, data: app.pushes.append((kind, copy.deepcopy(data)))
    app.bili = types.SimpleNamespace(login_uid=None, login_name="", connected=False,
                                     client=None, room_id=0)
    app.connect_calls = 0

    async def _connect():
        app.connect_calls += 1

    app.connect = _connect
    app.qr = types.SimpleNamespace(poll_calls=0)

    async def _qr_poll(key):
        app.qr.poll_calls += 1
        return (-1, "不该被调用", "")

    app.qr.poll = _qr_poll
    controller.reset()
    app.broadcast = Broadcast(app)
    app.dt = None
    # main.py 新增：/api/broadcast/stop 走 app.schedule.stop_now()（串行锁 + abort_start），
    # 用真实 DailyBroadcastSchedule 包 mock Broadcast，不 new App、不触网。
    from live_schedule import DailyBroadcastSchedule
    app.schedule = DailyBroadcastSchedule(app)

    from aiohttp.test_utils import TestServer, TestClient
    wapp = main_mod.build_web(app)
    client = TestClient(TestServer(wapp))
    await client.start_server()
    try:
        HDR = {"X-BVB": "1"}
        P = "/api/broadcast/"

        async def post(path, data=None, headers=None):
            return await client.request("POST", path,
                                        json=data if data is not None else {},
                                        headers=headers if headers is not None else HDR)

        async def get(path, headers=None):
            return await client.request("GET", path, headers=headers or {})

        # C1 无 X-BVB 的 POST start → 403，完全不触达平台
        controller.reset()
        resp = await client.request("POST", P + "start", json={**sel(), **START_ARGS}, headers={})
        assert_eq(resp.status, 403, "POST /broadcast/start 无 X-BVB 应 403")
        assert_eq(await resp.text(), "missing header", "403 文案")
        no_write(controller, "无标头 POST start")
        record("C1 POST /api/broadcast/start 缺 X-BVB → 403 零平台写", PASS)

        # C1b Host 伪装 → 403
        resp = await post(P + "preview", sel(), headers={"X-BVB": "1", "Host": "evil.example.com"})
        assert_eq(resp.status, 403, "伪造 Host 应 403")
        assert_eq(controller.preview_calls, [], "Host guard 失败不得触达 capture")
        record("C1b 非本机 Host → 403", PASS)

        # C2 有标头但 confirm=false → 拒绝公开开播、零平台写、不写 config
        controller.reset()
        saved_calls.clear()
        await post(P + "preview", sel())
        resp = await post(P + "start", {**sel(), **START_ARGS, "confirm": False})
        body = await resp.json()
        assert_true(not body.get("ok"), "confirm=false 竟开播")
        no_write(controller, "confirm=false")
        assert_eq(saved_calls, [], "被拒开播不得写 config")
        record("C2 有标头但 confirm=false → 不公开开播、不写config", PASS)

        # C3 GET sources：只读、契约字段
        controller.reset()
        sbody = await (await get(P + "sources")).json()
        assert_true(sbody.get("ok"), "sources 应 ok")
        assert_true("windows" in sbody and "audio" in sbody and "default_window" in sbody
                    and "default_audio" in sbody, "sources 契约字段缺失: " + str(list(sbody)))
        no_write(controller, "GET sources")
        assert_eq(controller.preview_calls, [], "GET sources 不得动预览")
        record("C3 GET /api/broadcast/sources 只读、含 windows/audio/defaults", PASS)

        # C4 GET status：只读、含 phase/previewing、不泄露
        st = await (await get(P + "status")).json()
        assert_true("phase" in st and "previewing" in st and "running" in st,
                    "status 字段缺失: " + str(st))
        assert_no_leak(st, "GET status")
        no_write(controller, "GET status")
        record("C4 GET /api/broadcast/status 只读不泄露", PASS)

        # C5 frame.jpg：无帧→204；有帧→200 no-store，不落盘
        controller.frame = None
        resp = await get(P + "frame.jpg")
        assert_eq(resp.status, 204, "无帧应 204")
        jpeg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"fakejpegbody" * 8
        controller.frame = jpeg
        resp = await get(P + "frame.jpg")
        assert_eq(resp.status, 200, "有帧应 200")
        assert_eq(resp.headers.get("Cache-Control"), "no-store", "frame 应 no-store")
        assert_eq(await resp.read(), jpeg, "frame 字节应与 capture 一致")
        dropped = [f for f in os.listdir(tmp_log) if f.lower().endswith((".jpg", ".jpeg"))]
        assert_eq(dropped, [], "frame.jpg 疑似落盘: " + str(dropped))
        no_write(controller, "GET frame")
        record("C5 frame.jpg 无帧204/有帧200+no-store、不落盘", PASS)

        # C6 完整 HTTP 开播 + 直播中账号互斥 + stop 闭环
        controller.reset()
        saved_calls.clear()
        resp = await post(P + "preview", sel())
        assert_true((await resp.json()).get("ok"), "HTTP preview 应成功")
        resp = await post(P + "start", {**sel(), **START_ARGS})
        body = await resp.json()
        assert_true(body.get("ok"), "HTTP start 应成功: " + str(body))
        assert_no_leak(body, "HTTP start 返回")
        await asyncio.sleep(0.05)
        assert_eq(app.cfg["room_id"], FAKE_ROOM_ID, "开播成功后 cfg room_id")
        assert_eq(len(saved_calls), 1, "start 成功应恰好一次 config.save")
        assert_eq(app.connect_calls, 1, "开播后应自动连自己直播间")
        st = await (await get(P + "status")).json()
        assert_eq(st["phase"], "live", "HTTP status live")
        assert_no_leak(st, "HTTP live status")
        lb = await (await post("/api/bili/logout")).json()
        assert_true(not lb.get("ok"), "直播中 logout 竟成功")
        assert_eq(app.cfg["bili_cookie"], PLACEHOLDER_COOKIE, "logout 被拒后 Cookie 不得清")
        cb = await (await post("/api/bili/cookie", {"cookie": ""})).json()
        assert_true(not cb.get("ok"), "直播中换 Cookie 竟成功")
        assert_eq(app.cfg["bili_cookie"], PLACEHOLDER_COOKIE, "换 Cookie 被拒后不得清")
        qb = await (await post("/api/bili/qrpoll", {"key": "k"})).json()
        assert_true(not qb.get("ok"), "直播中 qrpoll 竟放行")
        assert_eq(app.qr.poll_calls, 0, "直播中 qrpoll 不得真正 poll")
        pv = await (await post(P + "preview", sel(hwnd=1111, audio=8888))).json()
        assert_true(not pv.get("ok"), "直播中 preview 竟成功")
        sp = await (await post(P + "stop")).json()
        assert_true(sp.get("ok"), "HTTP stop 应成功")
        assert_eq(controller.end_calls, [FAKE_ROOM_ID], "HTTP stop end 自己房间")
        assert_eq(len(saved_calls), 1, "stop 不应再写 config")
        record("C6 HTTP开播/直播中禁切账号(qrpoll拒绝且不poll)/stop 闭环无泄露", PASS)

        # C7 停播后账号路由恢复
        ck = await (await post("/api/bili/cookie", {"cookie": PLACEHOLDER_COOKIE})).json()
        assert_true(ck.get("ok"), "停播后 cookie 应可写")
        record("C7 停播后账号路由恢复", PASS)

        # C8 account/areas GET：安全返回、零写
        controller.reset()
        acc = await (await get(P + "account")).json()
        assert_no_leak(acc, "GET account")
        ar = await (await get(P + "areas")).json()
        assert_true(ar.get("ok") and ar.get("areas"), "areas 契约: " + str(ar))
        no_write(controller, "GET account/areas")
        record("C8 GET account/areas 安全只读", PASS)
    finally:
        await client.close()
        config.save, config.load = orig_save, orig_load


# ---------------------------------------------------------------- D. UI 字符串断言
def phase_d():
    emit("== D. web/index.html 直播界面契约（字符串断言，不依赖行号）")
    try:
        html = (PROJECT / "web" / "index.html").read_text(encoding="utf-8")
    except Exception as exc:
        record("D.read index.html", FAIL, str(exc))
        return
    m = re.search(r"let broadcastLoginRequested.*?</script>", html, re.S)
    if not m:
        record("D.直播 UI 代码段存在", FAIL, "未找到 broadcast 段落")
        return
    section = m.group(0)
    checks = [
        ("D1 POST 助手带 X-BVB 标头",
         "'X-BVB': '1'" in html or '"X-BVB":"1"' in html.replace(" ", "")),
        ("D2 开播按钮先弹确认再提交 confirm:true",
         "broadcastStart" in section and "confirm(" in section and "confirm: true" in section),
        ("D3 直播 UI 段无 desktop/pywebview 回退",
         "pywebview" not in section and "desktop" not in section.lower()),
        ("D4 start/stop/preview 全走 /api/broadcast 路由",
         all(u in section for u in ("/api/broadcast/start", "/api/broadcast/stop",
                                    "/api/broadcast/preview", "/api/broadcast/status",
                                    "/api/broadcast/stream.mjpg"))),
        ("D5 停播提示保留游戏与语音",
         "游戏" in section and "语音" in section),
    ]
    for name, cond in checks:
        record(name, PASS if cond else FAIL, "" if cond else "字符串契约不满足")


# ---------------------------------------------------------------- 主入口
async def amain():
    t0 = time.time()
    for phase in (phase_a, run_broadcast_tests, phase_c):
        try:
            await phase()
        except (Exception, asyncio.CancelledError) as exc:
            record("phase " + getattr(phase, "__name__", "?") + " 意外中断", FAIL,
                   repr(exc) + "\n" + traceback.format_exc(limit=5))
    try:
        phase_d()
    except Exception as exc:
        record("phase phase_d 意外中断", FAIL, repr(exc))
    passed = sum(1 for _, s, _ in _results if s == PASS)
    failed = [n for n, s, _ in _results if s == FAIL]
    skipped = [n for n, s, _ in _results if s == SKIP]
    emit()
    emit("合计 {} 项：通过 {}，失败 {}，待验(SKIP) {}，用时 {:.1f}s".format(
        len(_results), passed, len(failed), len(skipped), time.time() - t0))
    for n in failed:
        emit("  FAIL -> " + n)
    for n in skipped:
        emit("  SKIP -> " + n)
    return 1 if failed else 0


def main():
    code = asyncio.run(amain())
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("\n".join(_log_lines) + "\n", encoding="utf-8")
        print("log written: " + str(LOG_PATH), file=sys.stderr)
    except Exception as exc:
        print("log write failed: " + str(exc), file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
