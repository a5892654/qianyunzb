r"""「源画面窗口」（给直播姬窗口采集用）离线验证：不启动真实浏览器、不联网、绝不碰平台开播接口。

运行：.venv\Scripts\python.exe tools\verify_source_window.py
"""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_broadcast
from live_broadcast import SOURCE_H, SOURCE_TITLE, SOURCE_W, Broadcast


def make_app(previewing=False, running=False):
    capture = SimpleNamespace(previewing=previewing, output_running=running, snapshot=lambda: {"previewing": previewing},
                              stop_preview=Mock(), start_preview=Mock(), pixel_scale=lambda: 1.0,
                              stop_output=Mock(), frame_jpeg=lambda: None,
                              compositor=None)
    app = SimpleNamespace(cfg={"web_port": 8787, "live2d": {"enabled": False}, "broadcast": {}},
                          push=Mock(), bili=SimpleNamespace(login_uid=None, login_name=""),
                          schedule=Mock())
    b = Broadcast(app)
    b.capture = capture
    b.preview = AsyncMock(return_value={"ok": True})
    b.stop_preview = AsyncMock(return_value={"ok": True})
    b.prepare_overlay = AsyncMock()
    b.stop_overlay = Mock()
    return b, app


class FakeProc:
    def __init__(self):
        self.alive = True
        self.terminated = False
        self.pid = 4242

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def wait(self, timeout):
        return 0

    def kill(self):
        self.alive = False


class SourceWindowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def appears(wins):
        """第一次列窗口是开窗口前的残留快照（空的），之后才出现我们刚起的那个。"""
        state = {"n": 0}

        def fake():
            state["n"] += 1
            return {"windows": [], "audio": []} if state["n"] == 1 else wins
        return fake

    async def start(self, b, proc=None):
        proc = proc or FakeProc()
        with patch.object(live_broadcast.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(Broadcast, "fit_source_window",
                          AsyncMock(return_value=(SOURCE_W, SOURCE_H))):
            result = await b.start_source({"hwnd": 1111, "audio_device": 2222})
        return result, popen, proc

    async def test_start_previews_then_opens_named_window(self):
        b, _ = make_app()
        result, popen, _ = await self.start(b)
        self.assertTrue(result["ok"])
        b.preview.assert_awaited_once_with({"hwnd": 1111, "audio_device": 2222})
        args = popen.call_args.args[0]
        self.assertTrue(str(args[1]).endswith("source_window.py"), args)
        self.assertIn("http://127.0.0.1:8787/source", args[2])
        self.assertEqual(args[3], SOURCE_TITLE)          # 直播姬按这个标题找窗口
        self.assertEqual(args[4:6], [str(SOURCE_W), str(SOURCE_H)])
        self.assertTrue(b.source_open)
        self.assertTrue(b.status()["source"])
        self.assertEqual(b.status()["source_title"], SOURCE_TITLE)

    async def test_client_area_fitted_to_output_pixels(self):
        """窗口尺寸吃的是逻辑像素：150% 缩放下按 1920 请求会拿到 2880 再被屏幕钳掉，
        画面先被网页拉伸、再被直播姬缩小，观众看到的就是糊。客户区必须正好等于输出。"""
        b, app = make_app()
        wins = {"windows": [{"hwnd": 555, "title": SOURCE_TITLE, "pid": 4242}], "audio": []}
        with patch.object(live_broadcast.subprocess, "Popen", return_value=FakeProc()), \
             patch.object(live_broadcast, "sources", side_effect=self.appears(wins)), \
             patch.object(live_broadcast, "fit_borderless_client",
                          return_value=(SOURCE_W, SOURCE_H)) as fit:
            result = await b.start_source({"hwnd": 1111, "audio_device": 2222})
        fit.assert_called_once_with(555, SOURCE_W, SOURCE_H)
        self.assertEqual(result["client"], [SOURCE_W, SOURCE_H])
        self.assertNotIn("客户区", str(app.push.call_args_list))

    async def test_cannot_capture_the_source_window_itself(self):
        """源画面窗口放的就是这条采集链的输出，采它等于自己喂自己——实测会让
        WGC 在 ntdll 里访问违例，整个服务说没就没。"""
        b, _ = make_app()
        wins = {"windows": [{"hwnd": 555, "title": SOURCE_TITLE, "pid": 4242}], "audio": []}
        with patch.object(live_broadcast, "find_edge", return_value="msedge"), \
             patch.object(live_broadcast.subprocess, "Popen", return_value=FakeProc()), \
             patch.object(live_broadcast, "sources", side_effect=self.appears(wins)), \
             patch.object(live_broadcast, "fit_borderless_client", return_value=(SOURCE_W, SOURCE_H)):
            await b.start_source({"hwnd": 1111, "audio_device": 2222})
        self.assertEqual(b.source_hwnd, 555)
        res = await Broadcast.preview(b, {"hwnd": 555, "audio_device": 2222})
        self.assertFalse(res["ok"])
        self.assertIn("不能采集它自己", res["error"])
        b.capture.start_preview.assert_not_called()
        await b.stop_source()
        self.assertEqual(b.source_hwnd, 0)

    async def test_wrong_client_area_says_so(self):
        b, app = make_app()
        target = (SOURCE_W, SOURCE_H)          # 假采集的 pixel_scale 是 1.0，逻辑=物理
        wins = {"windows": [{"hwnd": 555, "title": "别的窗口", "pid": 4242},
                            {"hwnd": 556, "title": SOURCE_TITLE, "pid": 4242}], "audio": []}
        with patch.object(live_broadcast.subprocess, "Popen", return_value=FakeProc()), \
             patch.object(live_broadcast, "sources", side_effect=self.appears(wins)), \
             patch.object(live_broadcast, "fit_borderless_client", return_value=(2564, 1455)) as fit:
            result = await b.start_source({"hwnd": 1111, "audio_device": 2222})
        self.assertEqual(result["client"], [2564, 1455])
        self.assertEqual(fit.call_args.args[0], 556)      # 只对得上标题的那个窗口
        self.assertEqual(fit.call_args.args[1:], target)  # 目标是折算后的逻辑尺寸
        self.assertEqual(fit.call_count, 1)               # 闭环校正在 fit_borderless_client 里迭代
        self.assertIn("客户区", str(app.push.call_args_list))

    async def test_ignores_same_titled_window_left_by_last_run(self):
        """服务崩过一轮后，桌面上可能还留着上一轮的同名窗口。窗口归属按「新出现的
        hwnd」认（子进程会 re-exec，按 pid 认会把我们自己的窗口当成别人的，
        然后在 6 秒后判定启动失败、把刚开好的窗口关掉）。"""
        b, _ = make_app()
        wins = {"windows": [{"hwnd": 777, "title": SOURCE_TITLE, "pid": 999}], "audio": []}
        with patch.object(live_broadcast, "sources", return_value=wins), \
             patch.object(live_broadcast, "_SOURCE_FIT_S", 0.2):
            stale = {w["hwnd"] for w in wins["windows"]}
            got = await b.fit_source_window(1.0, stale)
        self.assertEqual(got, (0, 0))
        with patch.object(live_broadcast, "sources", return_value=wins), \
             patch.object(live_broadcast, "fit_borderless_client",
                          return_value=(SOURCE_W, SOURCE_H)) as fit:
            self.assertEqual(await b.fit_source_window(1.0, set()), (SOURCE_W, SOURCE_H))
        fit.assert_called_once_with(777, SOURCE_W, SOURCE_H)

    async def test_second_start_does_not_open_another_window(self):
        b, _ = make_app()
        await self.start(b)
        result, popen, _ = await self.start(b)
        self.assertTrue(result["ok"] and result["already"])
        popen.assert_not_called()

    async def test_start_rejected_while_broadcasting(self):
        b, _ = make_app(previewing=True, running=True)
        b.capture.output_running = True
        result = await b.start_source({"hwnd": 1, "audio_device": 0})
        self.assertFalse(result["ok"])
        b.preview.assert_not_awaited()

    async def test_spawn_failure_undoes_only_its_own_preview(self):
        """起不来窗口时，预览是它自己起的就要收回去；用户本来就在预览则不动。"""
        b, _ = make_app()
        with patch.object(live_broadcast.subprocess, "Popen", side_effect=OSError("没有 python")):
            result = await b.start_source({"hwnd": 1111, "audio_device": 2222})
        self.assertFalse(result["ok"])
        self.assertIn("启动失败", result["error"])
        b.stop_preview.assert_awaited_once()          # 预览是它自己起的，要收回去

        b2, _ = make_app(previewing=True)             # 用户本来就在预览
        with patch.object(live_broadcast.subprocess, "Popen", side_effect=OSError("没有 python")):
            self.assertFalse((await b2.start_source({"hwnd": 1111, "audio_device": 2222}))["ok"])
        b2.stop_preview.assert_not_awaited()

    async def test_window_that_dies_immediately_is_reported(self):
        """WebView2 运行时缺失时子进程会秒退，不能假装窗口开好了。"""
        b, _ = make_app()
        proc = FakeProc()
        proc.alive = False                            # poll() 返回退出码
        wins = {"windows": [], "audio": []}
        with patch.object(live_broadcast.subprocess, "Popen", return_value=proc), \
             patch.object(live_broadcast, "sources", side_effect=self.appears(wins)), \
             patch.object(live_broadcast, "_SOURCE_FIT_S", 0.2):
            result = await b.start_source({"hwnd": 1111, "audio_device": 2222})
        self.assertFalse(result["ok"])
        self.assertIn("WebView2", result["error"])
        self.assertFalse(b.source_open)
        b.stop_preview.assert_awaited_once()          # 自己起的预览要收回去

    async def test_stop_closes_window_and_keeps_user_preview(self):
        b, _ = make_app()
        _, _, proc = await self.start(b)
        result = await b.stop_source()
        self.assertTrue(result["ok"])
        self.assertTrue(proc.terminated)
        self.assertFalse(b.source_open)
        b.stop_preview.assert_awaited_once()

        b2, _ = make_app(previewing=True)
        _, _, _ = await self.start(b2)
        await b2.stop_source()
        b2.stop_preview.assert_not_awaited()          # 预览不是它起的，别替用户关

    async def test_platform_start_blocked_while_source_open(self):
        b, _ = make_app()
        await self.start(b)
        result = await b.start({"hwnd": 1111, "audio_device": 2222, "title": "测试",
                                "area_id": 591, "confirm": True})
        self.assertFalse(result["ok"])
        self.assertIn("源画面", result["error"])
        self.assertIsNone(b.platform)

    async def test_user_closing_window_clears_state(self):
        b, _ = make_app()
        _, _, proc = await self.start(b)
        proc.alive = False                            # 用户自己把窗口点了关闭
        self.assertFalse(b.source_open)
        self.assertFalse(b.status()["source"])

    async def test_close_shuts_window_down(self):
        b, _ = make_app()
        _, _, proc = await self.start(b)
        b.stop = AsyncMock(return_value={"ok": True})
        await b.close()
        self.assertTrue(proc.terminated)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_exist_and_are_guarded(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        import main as main_mod

        b, app = make_app()
        app.broadcast = b
        b.start_source = AsyncMock(return_value={"ok": True, "title": SOURCE_TITLE})
        b.stop_source = AsyncMock(return_value={"ok": True})
        w = web.Application()
        routes = web.RouteTableDef()

        @routes.get("/source")
        async def source_page(_):
            return web.Response(text="ok", content_type="text/html")

        @web.middleware
        async def guard(request, handler):
            if request.method == "POST" and request.headers.get("X-BVB") != "1":
                return web.Response(status=403, text="missing header")
            return await handler(request)

        @routes.post("/api/broadcast/source")
        async def source_start(request):
            return web.json_response(await b.start_source(await request.json()))

        @routes.post("/api/broadcast/source/stop")
        async def source_stop(_):
            return web.json_response(await b.stop_source())

        w.middlewares.append(guard)
        w.add_routes(routes)
        client = TestClient(TestServer(w))
        await client.start_server()
        try:
            page = await client.get("/source")
            self.assertEqual(page.status, 200)
            blocked = await client.post("/api/broadcast/source", json={"hwnd": 1, "audio_device": 0})
            self.assertEqual(blocked.status, 403)
            b.start_source.assert_not_awaited()
            ok = await client.post("/api/broadcast/source", json={"hwnd": 1111, "audio_device": 2222},
                                   headers={"X-BVB": "1"})
            self.assertTrue((await ok.json())["ok"])
            b.start_source.assert_awaited_once_with({"hwnd": 1111, "audio_device": 2222})
            stopped = await client.post("/api/broadcast/source/stop", headers={"X-BVB": "1"})
            self.assertTrue((await stopped.json())["ok"])
        finally:
            await client.close()

    async def test_real_app_registers_the_routes(self):
        import inspect
        import main as main_mod
        src = inspect.getsource(main_mod.build_web)
        self.assertIn('"/source"', src)
        self.assertIn('"/api/broadcast/source"', src)
        self.assertIn('"/api/broadcast/source/stop"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
