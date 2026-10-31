"""人设接线回归：默认迁移、自定义保留、聊天与正在运行的游戏保持一致。"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from brain import Brain
from dtgame.commentary import GameCommentary
from dtgame.player import Player, SYSTEM
from main import App, build_web


class PersonaConfigTests(unittest.TestCase):
    def test_only_old_factory_persona_is_migrated_and_persisted(self):
        for old in (None, config.LEGACY_DEFAULT_PERSONA, "自定义：沉稳的解说员", ""):
            with self.subTest(old=old), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                data = {"ai": {"openai_model": "keep-model"}, "room_id": 123}
                if old is not None:
                    data["ai"]["persona"] = old
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                with patch.object(config, "CONFIG_PATH", str(path)):
                    saved = config.load()
                expected = config.DEFAULT_PERSONA if old in (None, config.LEGACY_DEFAULT_PERSONA) else old
                self.assertEqual(saved["ai"]["persona"], expected)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["ai"]["persona"], expected)
                self.assertEqual(saved["ai"]["openai_model"], "keep-model")
                self.assertEqual(saved["room_id"], 123)

    def test_game_fallback_rotates_more_than_two_lines_without_a_cooldown(self):
        chat = GameCommentary(clock=lambda: 100)
        for state in ({"screen": "battle"}, {"screen": "map"}):
            chat.recent.clear()
            lines = []
            for _ in range(10):
                line = chat.filler(state)
                self.assertTrue(chat.can_speak(state, line))
                self.assertLessEqual(len(line), 26)
                chat.spoken(state, line)
                lines.append(line)
            self.assertGreaterEqual(len(set(lines[:5])), 5)
            self.assertTrue(all(a != b for a, b in zip(lines, lines[1:])))


class PersonaFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_chat_backends_and_game_use_the_same_persona(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["ai"]["persona"] = "自定义人物：嘴硬心软，夸奖时小得意。"
        brain = Brain()
        for backend in ("qoder", "openai"):
            with self.subTest(backend=backend):
                cfg["ai"]["backend"] = backend
                with patch.object(getattr(brain, backend), "ask", AsyncMock(return_value="这句夸奖，我收下啦。")) as ask:
                    await brain.reply(cfg, cfg["events"]["danmaku"],
                                      {"type": "danmaku", "name": "小明", "text": "你真可爱"})
                ask.assert_awaited_once()
                self.assertIn(cfg["ai"]["persona"], ask.await_args.args[1])
        app = object.__new__(App)
        app.cfg = cfg
        game = Player(app._dt_ai(), Mock())
        prompt = game._system_prompt()
        self.assertIn(cfg["ai"]["persona"], prompt)
        self.assertTrue(prompt.startswith(SYSTEM))
        self.assertEqual(prompt.count(cfg["ai"]["persona"]), 1)

    async def test_saving_persona_updates_active_game_and_clears_old_chat_style(self):
        app = object.__new__(App)
        app.cfg = copy.deepcopy(config.DEFAULTS)
        app.bili = SimpleNamespace(client=None)
        app.brain = Brain()
        app.brain.openai.history.append({"role": "assistant", "content": "旧语气"})
        app.dt = Player(app._dt_ai(), Mock())
        decision = {"role": "assistant", "content": '{"action":"end","say":"旧解说"}'}
        app.dt.history.append(decision)
        app.dt.running = True
        app.dt.turns_taken = 12
        client = TestClient(TestServer(build_web(app)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        persona = "自定义人物：好胜的小可爱，偶尔说贴合现场的抽象比喻。"
        with patch("config.save") as save:
            response = await client.post("/api/config", json={"ai": {"persona": persona}}, headers={"X-BVB": "1"})
            self.assertEqual(response.status, 200)
            save.assert_called_once_with(app.cfg)
        self.assertIn(persona, app.brain.system_prompt(app.cfg))
        self.assertIn(persona, app.dt._system_prompt())
        self.assertFalse(app.brain.openai.history)
        self.assertEqual(app.dt.history, [decision])
        self.assertTrue(app.dt.running)
        self.assertEqual(app.dt.turns_taken, 12)
        app.brain.openai.history.append({"role": "assistant", "content": "新语气"})
        with patch("config.save"):
            await client.post("/api/config", json={"ai": {"max_chars": 35}}, headers={"X-BVB": "1"})
        self.assertEqual(len(app.brain.openai.history), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
