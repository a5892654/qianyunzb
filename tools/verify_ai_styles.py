"""AI 直播风格切换回归：配置迁移、提示词、主动互动、礼物感谢与热更新。"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import styles
from brain import Brain, gift_thanks
from dtgame.player import Player
from main import App, build_web
from tools.verify_proactive_interaction import enter, make_app, settle


class StyleConfigTests(unittest.TestCase):
    def test_old_configs_pick_style_from_the_actual_persona(self):
        cases = ((None, "default"), (config.LEGACY_DEFAULT_PERSONA, "default"),
                 ("", "default"), ("自定义：沉稳的解说员", "custom"))
        for old, expected_style in cases:
            with self.subTest(old=old), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                data = {"ai": {}}
                if old is not None:
                    data["ai"]["persona"] = old
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                with patch.object(config, "CONFIG_PATH", str(path)):
                    saved = config.load()
                self.assertEqual(saved["ai"]["style"], expected_style)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["ai"]["style"], expected_style)

    def test_effective_persona_follows_style_and_infers_missing_style(self):
        custom = "自定义人物：温柔的电台主持人。"
        self.assertEqual(styles.effective_persona({"style": "taffy", "persona": custom}),
                         styles.TAFFY_PERSONA)
        self.assertEqual(styles.effective_persona({"style": "default", "persona": custom}),
                         config.DEFAULT_PERSONA)
        self.assertEqual(styles.effective_persona({"style": "custom", "persona": custom}), custom)
        self.assertEqual(styles.effective_persona({"persona": custom}), custom)
        self.assertEqual(styles.effective_persona({}), config.DEFAULT_PERSONA)

    def test_taffy_pack_keeps_the_public_voice_without_turning_into_a_meow_template(self):
        persona = styles.TAFFY_PERSONA
        for expected in ("本鱼", "观众姥爷", "事故", "不讨论中之人", "不要用原主播的名字"):
            self.assertIn(expected, persona)
        self.assertIn("大部分时候用我", persona)
        self.assertIn("偶尔用本鱼", persona)
        self.assertIn("本鱼只是偶尔冒出来", styles.TAFFY_RULES)
        self.assertIn("不要连续两句都用本鱼", styles.TAFFY_RULES)
        self.assertNotIn("雏草姬", persona)
        self.assertIn("不要使用猫叫口癖", styles.TAFFY_RULES)
        for key in ("topics", "closings", "continuations", "event_templates", "supporter_templates",
                    "follow_templates", "follow_prompt", "gift_token_thanks", "gift_thanks_tiers"):
            blob = json.dumps(styles.TAFFY_PACK[key], ensure_ascii=False)
            self.assertNotIn("喵", blob, key)
            self.assertNotIn("雏草姬", blob, key)
        self.assertIn("观众姥爷", " ".join(styles.TAFFY_PACK["topics"]))
        self.assertIn("不为整活乱打", styles.TAFFY_GAME_RULES)
        self.assertNotIn("自称taffy", styles.TAFFY_PERSONA)
        self.assertGreaterEqual(len(styles.TAFFY_PACK["topics"]), 12)
        self.assertEqual(len(styles.ui_styles()), 3)
        self.assertEqual({item["id"] for item in styles.ui_styles()}, set(styles.VALID))


class StylePromptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["ai"]["style"] = "taffy"
        self.brain = Brain()

    async def test_chat_and_game_prompts_use_the_taffy_persona_and_rules(self):
        system = self.brain.system_prompt(self.cfg)
        self.assertIn(styles.TAFFY_PERSONA, system)
        self.assertIn(styles.TAFFY_RULES, system)
        self.assertNotIn(config.DEFAULT_PERSONA, system)
        self.cfg["ai"]["backend"] = "openai"
        self.brain.openai.ask = AsyncMock(return_value="事故现场报告喵")
        await self.brain.reply(self.cfg, self.cfg["events"]["danmaku"],
                               {"type": "danmaku", "name": "小明", "text": "主播翻车了"})
        _, called_system, prompt = self.brain.openai.ask.await_args.args
        self.assertIn(styles.TAFFY_PERSONA, called_system)
        self.assertIn(styles.TAFFY_PACK["reply_styles"][0], prompt)
        app = object.__new__(App)
        app.cfg = self.cfg
        game = Player(app._dt_ai(), Mock())
        game_prompt = game._system_prompt()
        self.assertIn(styles.TAFFY_PERSONA, game_prompt)
        self.assertIn(styles.TAFFY_GAME_RULES, game_prompt)
        self.assertEqual(game_prompt.count(styles.TAFFY_PERSONA), 1)

    async def test_stardew_own_persona_still_wins_and_gift_lines_follow_style(self):
        app = object.__new__(App)
        self.cfg["game"]["active"] = "stardew"
        self.cfg["stardew"]["persona"] = "星露谷专属人设。"
        app.cfg = self.cfg
        self.assertEqual(app._stardew_ai()["persona"], "星露谷专属人设。")
        ev = {"name": "小明", "num": 100, "gift": "小花花", "value": 200, "paid": True}
        taffy_intro, taffy_extra = gift_thanks(ev, self.cfg["ai"])
        default_intro, _ = gift_thanks(ev)
        self.assertIn("猫粮罐", taffy_intro)
        self.assertIn("翻车", taffy_extra)
        self.assertIn("token", default_intro)

    async def test_taffy_output_strips_any_meow_the_model_leaks(self):
        self.cfg["ai"]["backend"] = "openai"
        self.brain.openai.ask = AsyncMock(return_value="本鱼来啦喵！雏草姬、除草剂和小鲸子今天也一起玩喵。")
        text, _, _ = await self.brain.reply(
            self.cfg, self.cfg["events"]["danmaku"],
            {"type": "danmaku", "name": "小明", "text": "本鱼来了"})
        self.assertNotIn("喵", text)
        self.assertNotIn("雏草姬", text)
        self.assertNotIn("除草剂", text)
        self.assertNotIn("小鲸子", text)
        self.assertGreaterEqual(text.count("观众姥爷"), 3)
        self.assertIn("本鱼", text)

    async def test_enter_and_supporter_templates_follow_style_until_user_customizes(self):
        self.cfg["events"]["enter"]["mode"] = "template"
        text, _, _ = await self.brain.reply(self.cfg, self.cfg["events"]["enter"],
                                            {"type": "enter", "name": "小明"})
        taffy_lines = [line.replace("{name}", "小明")
                       for line in styles.TAFFY_PACK["event_templates"]["enter"].splitlines()]
        self.assertIn(text, taffy_lines)
        ev_cfg = {**self.cfg["events"]["enter"], "templates": "欢迎{name}来到我的直播间"}
        text, _, _ = await self.brain.reply(self.cfg, ev_cfg, {"type": "enter", "name": "小明"})
        self.assertEqual(text, "欢迎小明来到我的直播间")
        text, _, _ = await self.brain.reply(
            self.cfg, self.cfg["events"]["enter"],
            {"type": "enter", "name": "小明", "_gift_supporter": True})
        supporter_lines = [line.replace("{name}", "小明")
                           for line in styles.TAFFY_PACK["supporter_templates"].splitlines()]
        self.assertIn(text, supporter_lines)


class WelcomeVarietyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.brain = Brain()

    async def test_welcome_pool_is_varied_and_no_longer_a_fixed_cycle(self):
        self.assertGreaterEqual(len(config.ENTER_TEMPLATES.splitlines()), 20)
        self.assertGreaterEqual(len(config.SUPPORTER_WELCOME_TEMPLATES.splitlines()), 12)
        self.assertGreaterEqual(len(styles.TAFFY_PACK["event_templates"]["enter"].splitlines()), 20)
        self.assertGreaterEqual(len(styles.TAFFY_PACK["supporter_templates"].splitlines()), 12)
        texts = []
        for index in range(8):
            text, source, _ = await self.brain.reply(
                self.cfg, self.cfg["events"]["enter"], {"type": "enter", "name": f"观众{index}"})
            self.assertEqual(source, "模板")
            texts.append(text)
        self.assertGreaterEqual(len(set(texts)), 6)

    def test_legacy_factory_welcome_upgrades_while_custom_copy_survives(self):
        legacy = config._merge(config.DEFAULTS, {"events": {"enter": {
            "templates": config.LEGACY_ENTER_TEMPLATES[0],
            "supporter_templates": config.LEGACY_SUPPORTER_WELCOME_TEMPLATES[0],
            "ai_prompt": config.LEGACY_ENTER_AI_PROMPT}}})
        config.upgrade_legacy_texts(legacy)
        self.assertEqual(legacy["events"]["enter"]["templates"], config.ENTER_TEMPLATES)
        self.assertEqual(legacy["events"]["enter"]["supporter_templates"],
                         config.SUPPORTER_WELCOME_TEMPLATES)
        self.assertEqual(legacy["events"]["enter"]["ai_prompt"], config.ENTER_AI_PROMPT)
        custom = config._merge(config.DEFAULTS,
                               {"events": {"enter": {"templates": "我自己的欢迎{name}"}}})
        config.upgrade_legacy_texts(custom)
        self.assertEqual(custom["events"]["enter"]["templates"], "我自己的欢迎{name}")


class StyleInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_topics_follow_style_and_custom_follow_copy_is_preserved(self):
        app, now = make_app()
        app.cfg["ai"]["style"] = "taffy"
        enter(app)
        settle(app)
        self.assertEqual(app.pipe.interaction.topic_options("new"), styles.TAFFY_PACK["topics"])
        self.assertEqual(app.pipe.interaction.follow_templates(), styles.TAFFY_PACK["follow_templates"])
        self.assertEqual(app.pipe.interaction.follow_prompt(), styles.TAFFY_PACK["follow_prompt"])
        app.cfg["interaction"]["follow_templates"] = "我自己写的关注提醒"
        app.cfg["interaction"]["follow_ai_prompt"] = "我自己写的关注提示"
        self.assertEqual(app.pipe.interaction.follow_templates(), "我自己写的关注提醒")
        self.assertEqual(app.pipe.interaction.follow_prompt(), "我自己写的关注提示")
        now[0] += 21
        app.pipe.interaction.tick()
        ev = app.pipe.jobs[0]["ev"]
        await app.pipe.interaction.reply(ev)
        prompt = app.brain.openai.ask.await_args.args[2]
        self.assertTrue(any(line in prompt for line in styles.TAFFY_PACK["chat_styles"]))
        self.assertNotIn(styles.TAFFY_PACK["follow_prompt"], prompt)


class StyleApiTests(unittest.IsolatedAsyncioTestCase):
    async def client(self):
        app = object.__new__(App)
        app.cfg = copy.deepcopy(config.DEFAULTS)
        app.bili = SimpleNamespace(client=None)
        app.game = Mock()
        app.push = Mock()
        app.brain = Brain()
        app.brain.openai.history.append({"role": "assistant", "content": "旧语气"})
        app.dt = Player(app._dt_ai(), Mock())
        client = TestClient(TestServer(build_web(app)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return app, client

    async def test_switch_style_validates_and_updates_chat_game_and_history(self):
        app, client = await self.client()
        response = await client.get("/api/config")
        body = await response.json()
        self.assertEqual([item["id"] for item in body["ai"]["_styles"]],
                         ["default", "taffy", "custom"])
        self.assertIn(styles.TAFFY_PERSONA, body["ai"]["_styles"][1]["persona"])

        response = await client.post("/api/config", json={"ai": {"style": "unknown"}},
                                     headers={"X-BVB": "1"})
        self.assertEqual(response.status, 400)
        self.assertEqual(app.cfg["ai"]["style"], "default")

        with patch("config.save"):
            response = await client.post("/api/config", json={"ai": {"style": "taffy"}},
                                         headers={"X-BVB": "1"})
            self.assertEqual(response.status, 200)
        self.assertEqual(app.cfg["ai"]["style"], "taffy")
        self.assertIn(styles.TAFFY_PERSONA, app.brain.system_prompt(app.cfg))
        self.assertIn(styles.TAFFY_PERSONA, app.dt._system_prompt())
        self.assertIn(styles.TAFFY_GAME_RULES, app.dt._system_prompt())
        self.assertFalse(app.brain.openai.history)
        app.push.assert_called_with("log", ANY)

        with patch("config.save"):
            response = await client.post("/api/config", json={"ai": {"style": "default"}},
                                         headers={"X-BVB": "1"})
            self.assertEqual(response.status, 200)
        self.assertIn(config.DEFAULT_PERSONA, app.brain.system_prompt(app.cfg))
        self.assertNotIn(styles.TAFFY_PERSONA, app.dt._system_prompt())

    async def test_editing_persona_switches_to_custom_without_touching_style_presets(self):
        app, client = await self.client()
        persona = "自定义人物：沉稳的深夜电台主持人。"
        with patch("config.save"):
            response = await client.post("/api/config", json={"ai": {"persona": persona}},
                                         headers={"X-BVB": "1"})
            self.assertEqual(response.status, 200)
        self.assertEqual(app.cfg["ai"]["style"], "custom")
        self.assertIn(persona, app.brain.system_prompt(app.cfg))
        self.assertIn(persona, app.dt._system_prompt())
        self.assertEqual(app.cfg["ai"]["persona"], persona)


class StyleUiTests(unittest.TestCase):
    def test_settings_and_dashboard_both_expose_the_style_switch(self):
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        self.assertGreaterEqual(html.count('data-k="ai.style"'), 2)
        for expected in ('id="aiPersona"', 'id="dashAiStyle"', "renderAiStyle", "永雏塔菲风格",
                         "自定义人设", "只读预览"):
            self.assertIn(expected, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
