"""Live2D 模型切换回归；临时模型，不登录平台、不启动语音或直播。"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import live2d


class ModelFixtures:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a = self.make_model("旧模型", "old")
        self.b = self.make_model("新模型", "new")
        self.cfg = copy.deepcopy(config.DEFAULTS["live2d"])
        self.cfg.update(model_dir=str(self.a), model_file="runtime/variant.model3.json",
                        scale=1.7, lip_gain=2, base_expressions=["old"], idle_motion="old",
                        emotions=[{"name": "开心", "expressions": ["old"], "motion": "old"}])
        self.library = patch.object(live2d, "LIBRARY_DIR", str(self.root))
        self.library.start()
        self.addCleanup(self.library.stop)

    def make_model(self, name, asset):
        folder = self.root / name
        runtime = folder / "runtime"
        runtime.mkdir(parents=True)
        for name in ("avatar", "variant"):
            (runtime / f"{name}.model3.json").write_text(json.dumps({
                "Version": 3, "FileReferences": {"Moc": "avatar.moc3", "Textures": ["texture.png"]},
            }), encoding="utf-8")
        (runtime / f"{asset}.exp3.json").write_text('{"Parameters": []}', encoding="utf-8")
        (runtime / f"{asset}.motion3.json").write_text('{}', encoding="utf-8")
        return folder


class SwitchTests(ModelFixtures, unittest.TestCase):
    def test_model_defaults_apply_but_saved_settings_win(self):
        (self.b / "whaletalk.model.json").write_text(json.dumps({"lip_gain": 1.8,
            "parameter_map": {"angle_x": "AngleX"}, "base_expressions": ["new"],
            "emotions": [{"name": "恢复默认", "motion": "@reset", "expressions": []}]}), encoding="utf-8")
        live2d.switch_model(self.cfg, "新模型")
        self.assertEqual(self.cfg["parameter_map"], {"angle_x": "AngleX"})
        self.assertEqual(self.cfg["base_expressions"], ["new"])
        self.assertEqual(self.cfg["lip_gain"], 1.8)
        self.assertEqual(self.cfg["emotions"][0]["motion"], "@reset")
        self.cfg["lip_gain"] = 0.6
        live2d.switch_model(self.cfg, "旧模型")
        self.assertEqual(self.cfg["parameter_map"], {})
        live2d.switch_model(self.cfg, "新模型")
        self.assertEqual(self.cfg["lip_gain"], 0.6)

    def test_new_model_has_clean_defaults_and_shared_emotion_names(self):
        live2d.switch_model(self.cfg, "新模型")
        self.assertEqual(self.cfg["model_dir"], str(self.b))
        self.assertEqual(self.cfg["scale"], 1)
        self.assertEqual(self.cfg["lip_gain"], config.DEFAULTS["live2d"]["lip_gain"])
        self.assertEqual(self.cfg["base_expressions"], [])
        self.assertEqual(self.cfg["idle_motion"], "")
        self.assertIn("开心", [e["name"] for e in self.cfg["emotions"]])
        self.assertTrue(all(not e["expressions"] for e in self.cfg["emotions"]))

    def test_round_trip_restores_variant_base_expressions_and_lipsync(self):
        old = copy.deepcopy(self.cfg)
        live2d.switch_model(self.cfg, "新模型")
        self.cfg.update(scale=0.8, base_expressions=["new"], lip_gain=0.7)
        live2d.switch_model(self.cfg, "旧模型")
        for field in live2d.PROFILE_FIELDS:
            self.assertEqual(self.cfg[field], old[field], field)
        live2d.switch_model(self.cfg, "新模型")
        self.assertEqual(self.cfg["base_expressions"], ["new"])
        self.assertEqual(self.cfg["scale"], 0.8)
        self.assertEqual(self.cfg["lip_gain"], 0.7)

    def test_missing_saved_variant_falls_back_and_stale_assets_are_removed(self):
        self.cfg["profiles"] = {"新模型": {"model_file": "gone.model3.json",
            "base_expressions": ["old", "new"], "idle_motion": "old",
            "emotions": [{"name": "开心", "expressions": ["old", "new"], "motion": "old"}]}}
        live2d.switch_model(self.cfg, "新模型")
        self.assertEqual(self.cfg["model_file"], "runtime/avatar.model3.json")
        self.assertEqual(self.cfg["base_expressions"], ["new"])
        self.assertEqual(self.cfg["emotions"][0]["expressions"], ["new"])
        self.assertEqual(self.cfg["emotions"][0]["motion"], "")

    def test_invalid_or_unknown_model_leaves_active_config_untouched(self):
        (self.b / "runtime/avatar.model3.json").write_text("broken", encoding="utf-8")
        before = copy.deepcopy(self.cfg)
        for key, error in (("新模型", ValueError), ("../outside", FileNotFoundError)):
            with self.assertRaises(error):
                live2d.switch_model(self.cfg, key)
            self.assertEqual(self.cfg, before)

    def test_nested_resources_are_relative_to_library_model_root(self):
        live2d.switch_model(self.cfg, "新模型")
        refs = live2d.build_model_json(self.cfg)["FileReferences"]
        self.assertEqual(refs["Moc"], "runtime/avatar.moc3")
        self.assertEqual(refs["Textures"], ["runtime/texture.png"])
        self.assertEqual(refs["Motions"]["new"][0]["File"], "runtime/new.motion3.json")


class RouteTests(ModelFixtures, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        from main import build_web
        self.app = SimpleNamespace(cfg={"live2d": self.cfg}, push=Mock(),
                                   broadcast=SimpleNamespace(refresh_overlay=AsyncMock()))
        self.client = TestClient(TestServer(build_web(self.app)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_switch_saves_then_pushes_and_refreshes_broadcast_overlay(self):
        with patch("config.save") as save:
            response = await self.client.post("/api/l2d/switch", json={"key": "新模型"}, headers={"X-BVB": "1"})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["ok"])
        save.assert_called_once_with(self.app.cfg)
        self.app.push.assert_called_once_with("l2d_config", {})
        self.app.broadcast.refresh_overlay.assert_awaited_once()
        info = await (await self.client.get("/api/l2d/info")).json()
        self.assertEqual(info["error"], "")
        self.assertEqual([e["id"] for e in info["expressions"]], ["new"])

    async def test_bad_switch_never_saves_or_pushes(self):
        with patch("config.save") as save:
            response = await self.client.post("/api/l2d/switch", json={"key": "missing"}, headers={"X-BVB": "1"})
        self.assertEqual(response.status, 404)
        self.assertFalse((await response.json())["ok"])
        save.assert_not_called()
        self.app.push.assert_not_called()
        self.app.broadcast.refresh_overlay.assert_not_awaited()


class AiPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_deepseek_expression_and_motion_is_selectable_by_both_ai_backends(self):
        from brain import Brain
        preset = json.loads((Path(__file__).resolve().parents[1] / "docs/live2d/deepseek-chan.json").read_text(encoding="utf-8"))
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["live2d"].update(enabled=True, emotions=preset["emotions"])
        brain = Brain()
        self.assertEqual(len(preset["emotions"]), 44)
        self.assertEqual(len({e for emo in preset["emotions"] for e in emo["expressions"]}), 23)
        self.assertEqual(len({e["motion"] for e in preset["emotions"] if e["motion"] and e["motion"] != "@reset"}), 8)
        for backend in ("openai", "qoder"):
            cfg["ai"]["backend"] = backend
            with patch.object(getattr(brain, backend), "ask", new_callable=AsyncMock) as ask:
                for emotion in preset["emotions"]:
                    name = emotion["name"]
                    ask.return_value = f"【{name}】大家一起来玩吧！"
                    text, selected = await brain.ai(cfg, "请演示这个表情或动作")
                    self.assertEqual(selected, name, (backend, name))
                    self.assertEqual(text, "大家一起来玩吧！")
                    self.assertIn(name, ask.await_args.args[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
