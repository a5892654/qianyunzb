import asyncio
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import App, build_web
from tools.desktop_launcher import DesktopApi, DesktopController, SingleInstance, WebApp


def make_app():
    app = object.__new__(App)
    app.cfg = {"ai": {}, "audio": {}, "tts": {"stream_enabled": True, "qwen_enabled": True},
               "dtgame": {"openai_base": "http://127.0.0.1:1/v1", "openai_key": "test", "openai_model": "test"}}
    app.dt = None
    app.dt_task = None
    app.dt_resume_lock = asyncio.Lock()
    app.engine_ready = asyncio.Event()
    app.game = Mock()
    app.game.running.return_value = False
    app.pipe = Mock()
    app.push = Mock()
    app._bridge_alive = Mock(return_value=True)
    app._dt_bridge = Mock()
    app._dt_bridge.return_value.screen.return_value = {"screen": "battle"}
    return app


class PlayerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_marks_pending_player_running(self):
        app = make_app()
        self.assertTrue(app.start_dt()["ok"])
        self.assertTrue(app.dt.running)
        self.assertFalse(app.start_dt()["ok"])
        await app.stop_dt()

    async def test_pause_cancels_inflight_decision(self):
        app = make_app()
        entered = asyncio.Event()
        actions = []
        async def decision():
            entered.set()
            await asyncio.sleep(60)
            actions.append("late")
        app.dt = Mock()
        app.dt_task = asyncio.create_task(decision())
        task = app.dt_task
        await entered.wait()
        self.assertTrue((await app.stop_dt())["ok"])
        self.assertTrue(task.cancelled())
        self.assertEqual(actions, [])
        self.assertIsNone(app.dt)

    async def test_pause_idempotent(self):
        self.assertTrue((await make_app().stop_dt())["ok"])

    async def test_existing_game_does_not_restart(self):
        app = make_app()
        app.dt = Mock(running=True)
        self.assertTrue((await app.resume_dt())["already"])
        app.game.launch_and_wait.assert_not_called()

    async def test_resume_menu_loads_existing_save(self):
        app = make_app()
        bridge = app._dt_bridge.return_value
        bridge.screen.side_effect = [{"screen": "menu"}, {"screen": "battle"}]
        bridge.continue_run.return_value = {"ok": True}
        app.start_dt = Mock(return_value={"ok": True})
        self.assertTrue((await app.resume_dt())["ok"])
        bridge.continue_run.assert_called_once()
        app.start_dt.assert_called_once()

    async def test_menu_not_ready_retries_once(self):
        app = make_app()
        bridge = app._dt_bridge.return_value
        bridge.screen.side_effect = [{"screen": "menu"}, {"screen": "map"}]
        bridge.continue_run.side_effect = [{"ok": False, "error": "当前不在主菜单"}, {"ok": True}]
        app.start_dt = Mock(return_value={"ok": True})
        self.assertTrue((await app.resume_dt())["ok"])
        self.assertEqual(bridge.continue_run.call_count, 2)

    async def test_missing_save_requires_confirmation(self):
        app = make_app()
        bridge = app._dt_bridge.return_value
        bridge.screen.return_value = {"screen": "menu"}
        bridge.continue_run.return_value = {"ok": False, "no_save": True, "error": "没有存档"}
        app.start_dt = Mock()
        self.assertEqual((await app.resume_dt())["code"], "no_save")
        app.start_dt.assert_not_called()

    async def test_timeout_is_not_missing_save(self):
        app = make_app()
        bridge = app._dt_bridge.return_value
        bridge.screen.return_value = {"screen": "menu"}
        bridge.continue_run.return_value = {"ok": False, "error": "请处理账号验证"}
        app.start_dt = Mock()
        result = await app.resume_dt()
        self.assertEqual(result["code"], "resume_pending")
        self.assertEqual(result["error"], "请处理账号验证")
        app.start_dt.assert_not_called()

    async def test_hall_requires_new_run_confirmation(self):
        app = make_app()
        app._dt_bridge.return_value.screen.return_value = {"screen": "hall"}
        app.start_dt = Mock()
        self.assertEqual((await app.resume_dt())["code"], "no_save")
        app.start_dt.assert_not_called()

    async def test_confirmed_new_run_can_start(self):
        app = make_app()
        app._dt_bridge.return_value.screen.return_value = {"screen": "hall"}
        app.start_dt = Mock(return_value={"ok": True})
        self.assertTrue((await app.resume_dt(new_run=True))["ok"])

    async def test_bad_model_rejected_before_launch(self):
        app = make_app()
        app.cfg["dtgame"]["openai_key"] = ""
        self.assertFalse((await app.resume_dt())["ok"])
        app.game.launch_and_wait.assert_not_called()

    async def test_disconnected_game_recovered_before_start(self):
        app = make_app()
        app._bridge_alive.return_value = False
        app.game.launch_and_wait = AsyncMock(return_value={"ok": True})
        app.start_dt = Mock(return_value={"ok": True})
        self.assertTrue((await app.resume_dt())["ok"])
        app.game.launch_and_wait.assert_awaited_once()

    async def test_concurrent_resume_only_starts_once(self):
        app = make_app()
        def start():
            app.dt = Mock(running=True)
            return {"ok": True}
        app.start_dt = Mock(side_effect=start)
        await asyncio.gather(app.resume_dt(), app.resume_dt())
        app.start_dt.assert_called_once()


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        self.app = make_app()
        self.client = TestClient(TestServer(build_web(self.app)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_status_has_identity_and_real_stream_health(self):
        with patch("dtgame.ttsd.alive", AsyncMock(return_value=True)):
            response = await self.client.get("/api/desktop/status")
        result = await response.json()
        self.assertEqual(result["app"], "WhaleTalk")
        self.assertTrue(result["engine_ready"])

    async def test_post_requires_local_header(self):
        response = await self.client.post("/api/dtgame/resume", json={})
        self.assertEqual(response.status, 403)

    async def test_new_run_must_be_boolean(self):
        response = await self.client.post("/api/dtgame/resume", json={"new_run": "false"}, headers={"X-BVB": "1"})
        self.assertEqual(response.status, 400)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config.json").write_text(json.dumps({"web_port": 8787}), encoding="utf-8")
        (self.root / "logs").mkdir()
        self.ctrl = DesktopController(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_identity_required(self):
        self.ctrl._request = Mock(return_value={"app": "Other"})
        self.assertEqual(self.ctrl.status()[0], "occupied")

    def test_http_404_is_not_offline(self):
        self.ctrl._request = Mock(side_effect=urllib.error.HTTPError("local", 404, "not found", {}, None))
        self.assertEqual(self.ctrl.status()[0], "occupied")

    def test_service_start_no_second_ui_or_auto_run(self):
        before = (self.root / "config.json").read_bytes()
        with patch("subprocess.Popen") as popen:
            self.ctrl.start_service()
        args = popen.call_args.args[0]
        self.assertIn("--no-ui", args)
        self.assertIn("--manual-dt", args)
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08000000)
        self.assertEqual((self.root / "config.json").read_bytes(), before)

    def test_webview_uses_existing_complete_page(self):
        app = WebApp(self.root)
        app.ctrl = Mock(base="http://127.0.0.1:8787")
        app.shutdown = Mock()
        import webview
        with patch.object(webview, "create_window") as create, patch.object(webview, "start") as start:
            app.run()
        self.assertEqual(create.call_args.args[1], "http://127.0.0.1:8787/")
        self.assertIsInstance(create.call_args.kwargs["js_api"], DesktopApi)
        start.assert_called_once_with(gui="edgechromium", private_mode=True)
        app.shutdown.assert_called_once()

    def test_shutdown_calls_normal_quit_once(self):
        app = WebApp(self.root)
        app.ctrl = Mock()
        app.ctrl.status.return_value = ("ok", {})
        app.ctrl._request.return_value = {"ok": True}
        app.ctrl.wait_closed.return_value = True
        app.shutdown()
        app.shutdown()
        app.ctrl.quit.assert_called_once()

    def test_foreign_service_never_killed(self):
        app = WebApp(self.root)
        app.ctrl = Mock()
        app.ctrl.status.return_value = ("occupied", None)
        with self.assertRaises(RuntimeError):
            app.shutdown()
        app.ctrl.quit.assert_not_called()

    def test_close_destroys_only_after_cleanup(self):
        app = WebApp(self.root)
        app.shutdown = Mock()
        app.window = Mock()
        self.assertTrue(app.close()["ok"])
        app.shutdown.assert_called_once()
        app.window.destroy.assert_called_once()

    def test_js_api_only_exposes_close(self):
        api = DesktopApi(Mock())
        methods = [name for name in dir(api) if not name.startswith("_") and callable(getattr(api, name))]
        self.assertEqual(methods, ["close"])

    def test_single_instance_mutex_released(self):
        first, second = SingleInstance(self.root), SingleInstance(self.root)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
        finally:
            first.release()
            second.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)
