"""Model switching, ownership and streaming regression; no real GPU or platform."""
import asyncio
import contextlib
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import config
from dtgame import ttsd
from main import App, build_web
from tools.tts_server import SpeechService
import tts_models


class SelectionTests(unittest.TestCase):
    def test_legacy_model_is_inferred_and_selection_updates_both_backends(self):
        legacy = {"ttsd_model": r"C:\models\Qwen3-TTS-12Hz-1.7B-Base", "qwen_ref_audio": "original.wav"}
        self.assertEqual(tts_models.selected_size(legacy), "1.7B")
        value = {**legacy, **tts_models.selection_patch(legacy, "0.6B")}
        self.assertEqual(value["qwen_model"], "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
        self.assertIn("0.6B", value["ttsd_model"])
        self.assertEqual(value["qwen_ref_audio"], "original.wav")
        self.assertEqual(tts_models.selected_size(value), "0.6B")
        with self.assertRaises(ValueError):
            tts_models.selection_patch(legacy, "7B")

    def test_partial_download_is_not_marked_as_installed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "Qwen3-TTS-12Hz-0.6B-Base"
            model.mkdir()
            (model / "config.json").write_text("{}")
            with patch.object(tts_models, "MODEL_DIR", root):
                self.assertFalse(tts_models.choices({})[0]["downloaded"])
                (model / "model.safetensors").write_bytes(b"fixture")
                (model / "speech_tokenizer").mkdir()
                (model / "speech_tokenizer/model.safetensors").write_bytes(b"fixture")
                self.assertTrue(tts_models.choices({})[0]["downloaded"])
                self.assertEqual(tts_models.selection_patch({}, "0.6B")["ttsd_model"], str(model))

    def test_old_config_migration_does_not_mislabel_1_7b(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"tts": {"ttsd_model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base"}}))
            with patch.object(config, "CONFIG_PATH", str(path)):
                self.assertEqual(config.load()["tts"]["qwen_model_size"], "1.7B")


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_download_can_exceed_normal_model_load_timeout(self):
        clock = [0]
        async def sleep(seconds):
            clock[0] += 250
        with patch.object(ttsd, "_proc", None), patch.object(ttsd, "spawn", return_value=True), \
             patch.object(ttsd, "health", AsyncMock(side_effect=[{}, {"phase": "downloading"}, {"phase": "ready"}])), \
             patch.object(ttsd, "alive", AsyncMock(side_effect=[False, True])), \
             patch.object(ttsd, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
             patch.object(ttsd.asyncio, "sleep", sleep):
            self.assertTrue(await ttsd._ensure(config.DEFAULTS["tts"], Mock()))
        self.assertEqual(clock[0], 500)
    async def test_speech_is_not_sent_to_an_unverified_or_wrong_model(self):
        with patch.object(ttsd, "alive", AsyncMock(return_value=False)), \
             patch.object(ttsd.aiohttp, "ClientSession") as session:
            with self.assertRaisesRegex(RuntimeError, "所选模型"):
                async for _ in ttsd.synth_stream("test", config.DEFAULTS["tts"]):
                    pass
        session.assert_not_called()
    async def test_stop_during_health_probe_cancels_start_without_respawning(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def health(settings):
            entered.set()
            await release.wait()
            return {}
        with patch.object(ttsd, "_proc", None), patch.object(ttsd, "_signature", None), \
             patch.object(ttsd, "_generation", 0), patch.object(ttsd, "health", health), \
             patch.object(ttsd, "spawn") as spawn:
            task = asyncio.create_task(ttsd._ensure(config.DEFAULTS["tts"], Mock()))
            await entered.wait()
            ttsd.stop()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        spawn.assert_not_called()

    async def test_queued_start_keeps_model_snapshot_while_config_changes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        cfg = dict(config.DEFAULTS["tts"])
        original = cfg["ttsd_model"]
        async def ensure(settings, status):
            entered.set()
            await release.wait()
            return settings["ttsd_model"]
        with patch.object(ttsd, "_ensure", ensure), patch.object(ttsd, "_ensure_lock", asyncio.Lock()):
            task = asyncio.create_task(ttsd.ensure(cfg, Mock()))
            await entered.wait()
            cfg["ttsd_model"] = "changed-during-load"
            release.set()
            self.assertEqual(await task, original)

    async def test_switch_releases_owned_model_before_loading_replacement(self):
        cfg = copy.deepcopy(config.DEFAULTS["tts"])
        events = []
        old = SimpleNamespace(poll=lambda: None)

        def stop():
            events.append("stop")
            ttsd._proc = None
            ttsd._signature = None

        def spawn(settings, status):
            events.append("spawn")
            ttsd._proc = SimpleNamespace(poll=lambda: None)
            ttsd._signature = ttsd._desired(settings)
            return True

        with patch.object(ttsd, "_proc", old), patch.object(ttsd, "_signature", ("old",)), \
             patch.object(ttsd, "stop", side_effect=stop), patch.object(ttsd, "spawn", side_effect=spawn), \
             patch.object(ttsd, "health", AsyncMock(return_value={})), \
             patch.object(ttsd, "alive", AsyncMock(return_value=True)), \
             patch.object(ttsd.asyncio, "sleep", AsyncMock()):
            self.assertTrue(await ttsd._ensure(cfg, Mock()))
        self.assertEqual(events, ["stop", "spawn"])

    async def test_external_wrong_model_is_not_killed_or_called_ready(self):
        cfg = copy.deepcopy(config.DEFAULTS["tts"])
        state = {"ok": True, "model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base"}
        with patch.object(ttsd, "_proc", None), patch.object(ttsd, "health", AsyncMock(return_value=state)), \
             patch.object(ttsd, "stop") as stop, patch.object(ttsd, "spawn") as spawn:
            self.assertFalse(await ttsd.alive(cfg))
            self.assertFalse(await ttsd._ensure(cfg, Mock()))
        stop.assert_not_called()
        spawn.assert_not_called()

    async def test_same_owned_model_is_reused_without_double_load(self):
        cfg = copy.deepcopy(config.DEFAULTS["tts"])
        with patch.object(ttsd, "_proc", SimpleNamespace(poll=lambda: None)), \
             patch.object(ttsd, "_signature", ttsd._desired(cfg)), \
             patch.object(ttsd, "health", AsyncMock(return_value={"ok": True, "model": cfg["ttsd_model"]})), \
             patch.object(ttsd, "stop") as stop, patch.object(ttsd, "spawn") as spawn:
            self.assertTrue(await ttsd._ensure(cfg, Mock()))
        stop.assert_not_called()
        spawn.assert_not_called()


class ModelRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = object.__new__(App)
        self.app.cfg = copy.deepcopy(config.DEFAULTS)
        self.app.tasks = []
        self.app.pipe = SimpleNamespace(current=None, tts_ready=asyncio.Event())
        self.app.pipe.tts_ready.set()
        self.app.engine = SimpleNamespace(proc=None, comfy_proc=None, stop=Mock())
        self.app.push = Mock()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "fixture"
        self.file.write_text("fixture")
        self.app.cfg["tts"]["qwen_ref_audio"] = str(self.file)
        self.patches = [patch("config.save"), patch.object(ttsd, "runtime_paths", return_value=(str(self.file), str(self.file))),
                        patch.object(ttsd, "health", AsyncMock(return_value={}))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(TestServer(build_web(self.app)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def post(self, body, path="/api/tts/model"):
        return await self.client.post(path, json=body, headers={"X-BVB": "1"})

    async def test_busy_speech_and_invalid_sizes_preserve_settings(self):
        before = copy.deepcopy(self.app.cfg)
        bad = await self.post({"size": "7B"})
        self.assertEqual(bad.status, 400)
        self.app.pipe.current = {"id": "speaking"}
        busy = await self.post({"size": "1.7B"})
        self.assertEqual(busy.status, 409)
        self.assertEqual(self.app.cfg, before)

    async def test_switch_gates_speech_and_synchronizes_both_backends(self):
        release = asyncio.Event()
        async def ensure(settings, status):
            await release.wait()
            return True
        with patch.object(ttsd, "ensure", ensure):
            response = await self.post({"size": "1.7B"})
            self.assertTrue((await response.json())["loading"])
            self.assertFalse(self.app.pipe.tts_ready.is_set())
            duplicate = await self.post({"size": "0.6B"})
            self.assertEqual(duplicate.status, 409)
            release.set()
            await self.app.tasks[-1]
        self.assertTrue(self.app.pipe.tts_ready.is_set())
        self.assertEqual(self.app.cfg["tts"]["qwen_model_size"], "1.7B")
        self.assertIn("1.7B", self.app.cfg["tts"]["qwen_model"])
        self.assertIn("1.7B", self.app.cfg["tts"]["ttsd_model"])
        self.assertEqual(self.app.cfg["tts"]["qwen_ref_audio"], str(self.file))

    async def test_missing_reference_saves_selection_without_false_ready(self):
        self.app.cfg["tts"]["qwen_ref_audio"] = "reference-on-other-device.wav"
        with patch.object(ttsd, "ensure", AsyncMock()) as ensure:
            response = await self.post({"size": "0.6B"})
            result = await response.json()
        self.assertTrue(result["ok"])
        self.assertFalse(result["ready"])
        self.assertIn("参考音频", result["message"])
        ensure.assert_not_called()

    async def test_stop_cancels_switch_and_releases_gate_and_owned_engine(self):
        async def ensure(*args):
            await asyncio.Event().wait()
        with patch.object(ttsd, "ensure", ensure), patch.object(ttsd, "owned", return_value=True):
            await self.post({"size": "0.6B"})
            await asyncio.sleep(0)
            result = await (await self.post({}, "/api/tts/stop")).json()
        self.assertTrue(result["ok"])
        self.assertTrue(self.app.pipe.tts_ready.is_set())
        self.app.engine.stop.assert_called_once()

    async def test_status_does_not_claim_different_external_model_is_ready(self):
        with patch.object(ttsd, "health", AsyncMock(return_value={"ok": True, "model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base"})):
            result = await (await self.client.get("/api/tts/models")).json()
        self.assertFalse(result["ready"])


class StreamingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_speech_waits_until_model_switch_finishes(self):
        from pipeline import Pipeline
        app = SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), push=Mock(), dt=None)
        app.cfg["tts"]["stream_enabled"] = False
        app.engine = SimpleNamespace(synth=AsyncMock(return_value=(np.zeros((120, 1), np.int16), 24000)))
        pipeline = Pipeline(app, supporters=Mock())
        pipeline.tts_ready.clear()
        await pipeline.synth_q.put({"id": 1, "type": "manual", "text": "wait for new model"})
        task = asyncio.create_task(pipeline._synth_worker())
        try:
            await asyncio.sleep(0.03)
            app.engine.synth.assert_not_awaited()
            pipeline.tts_ready.set()
            await asyncio.wait_for(pipeline.play_q.get(), 1)
            app.engine.synth.assert_awaited_once()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_pcm_stream_has_correct_rate_and_uses_cached_reference(self):
        args = SimpleNamespace(model="fixture-0.6B", ref_audio="original.wav", language="Chinese", chunk_size=8)
        service = SpeechService(args)
        seen = []
        def stream(**kwargs):
            seen.append(kwargs)
            yield np.array([0, 0.5, -0.5], np.float32), 24000, {}
        service.model = SimpleNamespace(generate_voice_clone_streaming=stream)
        service.prompt, service.phase = ["cached-reference"], "ready"
        app = web.Application()
        app.router.add_post("/say", service.say)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            fake_torch = SimpleNamespace(inference_mode=contextlib.nullcontext)
            with patch.dict(sys.modules, {"torch": fake_torch}):
                response = await client.post("/say", json={"text": "测试"})
                pcm = await response.read()
            self.assertEqual(response.headers["X-Sample-Rate"], "24000")
            np.testing.assert_array_equal(np.frombuffer(pcm, "<i2"), [0, 16383, -16383])
            self.assertEqual(seen[0]["voice_clone_prompt"], service.prompt)
            self.assertTrue(seen[0]["xvec_only"])
        finally:
            await client.close()
            service.executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
