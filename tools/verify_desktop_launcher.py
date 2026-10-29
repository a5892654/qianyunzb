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


class ConfigWriteGuardMixin:
    """假 cfg 绝不能落盘：一旦写进真实 config.json，用户的登录 cookie、皮套、
    AI 接口配置会全被默认值盖掉。"""

    def _guard_config_writes(self):
        patcher = patch("main.config.save")
        self.save = patcher.start()
        self.addCleanup(patcher.stop)


class PlayerLifecycleTests(ConfigWriteGuardMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._guard_config_writes()

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

    async def test_recent_chars_persist_across_restarts(self):
        """换角色这件事必须跨重启记住：进程一开就忘了上一局是谁，又会连着点同一个人。"""
        app = make_app()
        app._remember_chars(["奈尔", "爱丽丝"])
        self.assertEqual(app.cfg["dtgame"]["recent_chars"], ["奈尔", "爱丽丝"])
        self.save.assert_called_once()
        self.save.reset_mock()
        app._remember_chars(["奈尔", "爱丽丝"])     # 没变就别反复写盘
        self.save.assert_not_called()

    async def test_player_starts_with_saved_chars(self):
        app = make_app()
        app.cfg["dtgame"]["recent_chars"] = ["奈尔"]
        with patch("main.DTPlayer") as made:
            app.start_dt()
        self.assertEqual(list(made.call_args.kwargs["chars"]), ["奈尔"])
        self.assertIs(made.call_args.kwargs["speak"], app.pipe.narrate)
        made.call_args.kwargs["on_chars"](["奈尔", "安洁莉亚"])
        self.assertEqual(app.cfg["dtgame"]["recent_chars"], ["奈尔", "安洁莉亚"])
        await app.stop_dt()

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

    async def test_missing_save_starts_new_run_by_itself(self):
        """没有可续的存档时别再要人确认：直接开新局，并在日志里说清楚。"""
        app = make_app()
        bridge = app._dt_bridge.return_value
        bridge.screen.return_value = {"screen": "menu"}
        bridge.continue_run.return_value = {"ok": False, "no_save": True, "error": "没有存档"}
        app.start_dt = Mock(return_value={"ok": True})
        self.assertTrue((await app.resume_dt())["ok"])
        app.start_dt.assert_called_once()
        self.assertIn("自动开始新出征", str(app.push.call_args_list))

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

    async def test_hall_starts_new_run_by_itself(self):
        """战斗中没有落盘点，重启后回到大厅是常态：自己开新局，别停在原地等人点。"""
        app = make_app()
        app._dt_bridge.return_value.screen.return_value = {"screen": "hall"}
        app.start_dt = Mock(return_value={"ok": True})
        self.assertTrue((await app.resume_dt())["ok"])
        app.start_dt.assert_called_once()
        self.assertIn("自动开始新出征", str(app.push.call_args_list))

    async def test_new_run_flag_still_tries_continue(self):
        app = make_app()
        app._dt_bridge.return_value.screen.return_value = {"screen": "hall"}
        app.start_dt = Mock(return_value={"ok": True})
        self.assertTrue((await app.resume_dt(new_run=True))["ok"])
        self.assertEqual(app._dt_bridge.return_value.continue_run.call_count, 0)

    async def test_schedule_prep_launches_game_then_resumes(self):
        app = make_app()
        app._bridge_alive = Mock(return_value=False)
        app.game.launch_and_wait = AsyncMock(return_value={"ok": True})
        app.resume_dt = AsyncMock(return_value={"ok": True})
        result = await app.ensure_game_and_dt()
        self.assertTrue(result["ok"] and result["launched"])
        app.game.launch_and_wait.assert_awaited_once()
        app.resume_dt.assert_awaited_once()

    async def test_schedule_prep_keeps_running_game_and_skips_launch(self):
        app = make_app()
        app.resume_dt = AsyncMock(return_value={"ok": True, "already": True})
        result = await app.ensure_game_and_dt()
        self.assertTrue(result["ok"] and not result.get("launched"))
        app.game.launch_and_wait.assert_not_called()

    async def test_schedule_prep_tolerates_dt_failure_but_warns(self):
        """代打起不来（比如模型没配）只警告：直播画面还是要照常开出去。"""
        app = make_app()
        app.game.launch_and_wait = AsyncMock(return_value={"ok": True})
        app.resume_dt = AsyncMock(return_value={"ok": False, "error": "还没填 API Key"})
        self.assertTrue((await app.ensure_game_and_dt())["ok"])
        self.assertIn("还没填 API Key", str(app.push.call_args_list))

    async def test_schedule_prep_fails_when_game_cannot_launch(self):
        app = make_app()
        app._bridge_alive = Mock(return_value=False)
        app.game.launch_and_wait = AsyncMock(return_value={"ok": False, "error": "找不到 Godot"})
        app.resume_dt = AsyncMock()
        result = await app.ensure_game_and_dt()
        self.assertFalse(result["ok"])
        self.assertIn("找不到 Godot", result["error"])
        app.resume_dt.assert_not_awaited()

    async def test_schedule_prep_does_not_launch_when_auto_launch_off(self):
        app = make_app()
        app._bridge_alive = Mock(return_value=False)
        app.cfg["dtgame"]["auto_launch"] = False
        result = await app.ensure_game_and_dt()
        self.assertFalse(result["ok"])
        self.assertIn("自动启动", result["error"])
        app.game.launch_and_wait.assert_not_called()

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


class CharacterRotationTests(unittest.TestCase):
    """代打换角色：模型没有跨局记忆，不记着上一局是谁，她可以连点十局同一个人。"""

    ROSTER = {"character_select": {"characters": [
        {"slot": 0, "id": "a", "name": "奈尔"},
        {"slot": 1, "id": "b", "name": "爱丽丝"},
        {"slot": 2, "id": "c", "name": "安洁莉亚"}]}}

    def player(self, used):
        from dtgame.player import Player
        p = Player({"openai_base": "http://127.0.0.1:1/v1", "openai_model": "m"},
                   Mock(), narrate=False)
        p.log_push = Mock()
        p.chars = list(used)
        return p

    def test_remembers_each_run_once(self):
        p = self.player([])
        for _ in range(3):
            p._remember_character({"run": {"character": "奈尔"}})
        p._remember_character({"run": {"character": "爱丽丝"}})
        self.assertEqual(p.chars, ["奈尔", "爱丽丝"])

    def test_title_form_is_the_same_character(self):
        """选角界面写「奈尔」，局内状态写「龙枪骑士·奈尔」，不能算成两个人。"""
        p = self.player(["奈尔"])
        p._remember_character({"run": {"character": "龙枪骑士·奈尔"}})
        self.assertEqual(p.chars, ["奈尔"])
        self.assertEqual(p._swap_if_repeat(self.ROSTER, 0, ""), 1)

    def test_hint_names_who_to_avoid_and_who_is_fresh(self):
        tip = self.player(["奈尔", "爱丽丝"])._rotation(self.ROSTER)
        self.assertIn("奈尔", tip)
        self.assertIn("必须换", tip)
        self.assertIn("安洁莉亚", tip)          # 没上过的要被点名

    def test_repeat_pick_gets_swapped(self):
        p = self.player(["奈尔"])
        self.assertEqual(p._swap_if_repeat(self.ROSTER, 0, ""), 1)
        self.assertIn("换成", str(p.log_push.call_args_list))
        p.log_push.reset_mock()
        self.assertEqual(p._swap_if_repeat(self.ROSTER, 2, ""), 2)   # 本来就换了就不动
        p.log_push.assert_not_called()

    def test_different_pick_is_kept_when_everyone_already_used(self):
        p = self.player(["奈尔", "爱丽丝", "安洁莉亚"])
        self.assertEqual(p._swap_if_repeat(self.ROSTER, 0, ""), 0)
        p.log_push.assert_not_called()

    def test_repeat_is_swapped_even_after_everyone_has_played(self):
        p = self.player(["奈尔", "爱丽丝", "安洁莉亚"])
        self.assertEqual(p._swap_if_repeat(self.ROSTER, 2, "c"), 0)

    def test_replacement_uses_least_recent_character(self):
        p = self.player(["爱丽丝", "奈尔", "安洁莉亚"])
        self.assertEqual(p._swap_if_repeat(self.ROSTER, 2, "c"), 1)

    def test_slot_and_id_both_change_in_actual_bridge_call(self):
        p = self.player(["奈尔"])
        p.bridge.pick_character.side_effect = lambda slot, cid: {"ok": True, "selected_id": cid}
        action = {"action": "character", "slot": 0, "id": "a"}
        result = p._do({"screen": "character_select", **self.ROSTER}, action)
        p.bridge.pick_character.assert_called_once_with(1, "b")
        self.assertEqual(result["selected_id"], "b")
        self.assertEqual((action["slot"], action["id"]), (1, "b"))

    def test_id_only_choice_can_be_rotated(self):
        p = self.player(["奈尔"])
        p.bridge.pick_character.return_value = {"ok": True}
        p._do({"screen": "character_select", **self.ROSTER}, {"action": "character", "id": "a"})
        p.bridge.pick_character.assert_called_once_with(1, "b")

    def test_conflicting_model_slot_is_normalized_to_id(self):
        p = self.player(["安洁莉亚"])
        p.bridge.pick_character.return_value = {"ok": True}
        p._do({"screen": "character_select", **self.ROSTER},
              {"action": "character", "slot": 0, "id": "b"})
        p.bridge.pick_character.assert_called_once_with(1, "b")

    def test_preview_character_does_not_replace_last_run(self):
        p = self.player(["爱丽丝"])
        p.on_chars = Mock()
        for screen in ("character_select", "hall", "menu"):
            p._remember_character({"screen": screen, "run": {"character": "奈尔"}})
        self.assertEqual(p.chars, ["爱丽丝"])
        p.on_chars.assert_not_called()

    def test_rotation_never_selects_locked_alternative(self):
        import copy
        state = copy.deepcopy(self.ROSTER)
        state["character_select"]["characters"][1]["locked"] = True
        p = self.player(["奈尔"])
        p.bridge.pick_character.return_value = {"ok": True}
        p._do({"screen": "character_select", **state}, {"action": "character", "slot": 0})
        p.bridge.pick_character.assert_called_once_with(2, "c")

    def test_one_available_character_can_continue(self):
        import copy
        state = copy.deepcopy(self.ROSTER)
        state["character_select"]["characters"][1]["locked"] = True
        state["character_select"]["characters"][2]["unlocked"] = False
        p = self.player(["奈尔"])
        p.bridge.pick_character.return_value = {"ok": True}
        p._do({"screen": "character_select", **state}, {"action": "character", "slot": 0})
        p.bridge.pick_character.assert_called_once_with(0, "a")
        self.assertIn("只有奈尔可选", p._rotation(state))

    def test_invalid_or_locked_choice_is_replaced_with_available_character(self):
        import copy
        state = {"screen": "character_select", **copy.deepcopy(self.ROSTER)}
        state["character_select"]["characters"][1]["enabled"] = False
        for action in ({"action": "character", "slot": 99},
                       {"action": "character", "slot": 0, "id": "missing"},
                       {"action": "character", "slot": 1}):
            p = self.player(["奈尔"])
            p.bridge.pick_character.return_value = {"ok": True}
            self.assertTrue(p._do(state, action)["ok"])
            p.bridge.pick_character.assert_called_once_with(2, "c")


class CharacterRotationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_model_choice_rotates_over_multiple_runs(self):
        from dtgame.player import Player
        import copy

        bridge = Mock()
        bridge.screen.side_effect = lambda: {"screen": "character_select" if bridge.state.return_value.get("screen") == "character_select" else "map"}
        bridge.pick_character.return_value = {"ok": True}
        def enter_map(*args):
            bridge.state.return_value = {"screen": "map"}
            return {"ok": True}
        bridge.pick_character.side_effect = enter_map
        on_chars, speak = Mock(), Mock(return_value=None)
        p = Player({}, bridge, chars=["奈尔"], on_chars=on_chars, speak=speak)
        p._ask = AsyncMock(side_effect=lambda text: {"action": "character", "slot": 0,
                                                    "id": "a", "say": "还是选奈尔吧"})
        for expected_slot, expected_id, expected_name in ((1, "b", "爱丽丝"), (0, "a", "奈尔"),
                                                          (2, "c", "安洁莉亚"), (0, "a", "奈尔")):
            state = {"screen": "character_select", **copy.deepcopy(CharacterRotationTests.ROSTER),
                     "run": {"character": "奈尔"}}  # 选角界面的旧预览值不能污染记录。
            bridge.state.return_value = state
            result = await p.step()
            self.assertTrue(result["ok"])
            bridge.pick_character.assert_called_with(expected_slot, expected_id)
            self.assertEqual(result["action"]["id"], expected_id)
            self.assertEqual(p.chars[-1], expected_name)  # 成功选角后立即持久化，不等下一帧。
            if expected_id != "a":
                self.assertIn(expected_name, result["say"])
                self.assertNotIn("还是选奈尔", result["say"])
            p._remember_character({"screen": "battle", "run": {"character": expected_name}})
        self.assertEqual(on_chars.call_args.args[0][-4:], ["爱丽丝", "奈尔", "安洁莉亚", "奈尔"])
        # 重新创建决策器时沿用落盘记录，仍然避开上一局。
        restarted = Player({}, bridge, chars=on_chars.call_args.args[0], narrate=False)
        action = {"action": "character", "slot": 0, "id": "a"}
        restarted._do(state, action)
        bridge.pick_character.assert_called_with(1, "b")

    async def test_failed_selection_does_not_record_preview_or_requested_character(self):
        from dtgame.player import Player
        bridge = Mock()
        bridge.screen.return_value = {"screen": "character_select"}
        bridge.state.return_value = {"screen": "character_select", **CharacterRotationTests.ROSTER,
                                     "run": {"character": "安洁莉亚"}}
        bridge.pick_character.return_value = {"ok": False, "error": "选角被拒绝"}
        on_chars = Mock()
        p = Player({}, bridge, chars=["奈尔"], on_chars=on_chars, narrate=False)
        p._ask = AsyncMock(return_value={"action": "character", "slot": 1, "say": "这局试试爱丽丝"})
        result = await p.step()
        self.assertFalse(result["ok"])
        self.assertEqual(p.chars, ["奈尔"])
        on_chars.assert_not_called()


class ApiTests(ConfigWriteGuardMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        self._guard_config_writes()
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
