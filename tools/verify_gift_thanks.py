"""礼物分档感谢离线回归：真实礼物事件、合并和语音队列，模型使用 mock。"""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bili import _Handler
from blivedm.models.web import GiftMessage
from brain import Brain, GIFT_TOKEN_THANKS, gift_thanks
from config import DEFAULTS, merge
from pipeline import Pipeline


def gift(**overrides):
    return {"type": "gift", "uid": 123, "name": "小明", "gift": "小花花",
            "num": 1, "value": 1, "paid": True, **overrides}


class GiftThanksTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["ai"]["backend"] = "openai"
        self.brain = Brain()
        self.brain.openai.ask = AsyncMock(return_value="谢谢")
        self.brain.qoder.ask = AsyncMock(return_value="谢谢")

    async def reply(self, ev, **settings):
        return await self.brain.reply(self.cfg, {**self.cfg["events"]["gift"], **settings}, ev)

    async def test_small_gift_names_donor_and_says_token_line_once(self):
        text, _, _ = await self.reply(gift())
        self.assertIn("谢谢小明老板送的1个小花花", text)
        self.assertEqual(text.count(GIFT_TOKEN_THANKS), 1)
        self.assertEqual(text.count("小明"), 1)
        self.brain.openai.ask.assert_not_awaited()

    async def test_quantity_and_total_value_each_increase_richness(self):
        by_count = [(await self.reply(gift(num=n, value=0), mode="template"))[0]
                    for n in (1, 5, 20, 100)]
        by_value = [(await self.reply(gift(num=1, value=v), mode="template"))[0]
                    for v in (1, 10, 50, 200)]
        for samples in (by_count, by_value):
            self.assertTrue(all(len(a) < len(b) for a, b in zip(samples, samples[1:])))
            self.assertNotIn("比个心", samples[1])
            self.assertIn("比个心", samples[2])
            self.assertIn("天天开心", samples[3])

    async def test_boundaries_do_not_upgrade_early(self):
        for count, value in ((4, 9.99), (19, 49.99), (99, 199.99)):
            below = (await self.reply(gift(num=count, value=value), mode="template"))[0]
            at_count = (await self.reply(gift(num=count + 1, value=value), mode="template"))[0]
            at_value = (await self.reply(gift(num=count, value=round(value + .01, 2)), mode="template"))[0]
            self.assertLess(len(below), len(at_count))
            self.assertLess(len(below), len(at_value))

    async def test_free_gifts_scale_by_count_without_paid_value(self):
        one = gift(paid=False, value=999)
        text, _, _ = await self.reply(one)
        self.assertNotIn("天天开心", text)
        many, _, _ = await self.reply(gift(paid=False, value=0, num=100), mode="template")
        self.assertIn("天天开心", many)

    async def test_ai_failure_empty_and_short_output_keep_full_tier(self):
        ev = gift(value=200)
        expected = "".join(gift_thanks(ev))
        for result in ("", "谢谢", RuntimeError("模型离线")):
            self.brain.openai.ask.side_effect = result if isinstance(result, Exception) else None
            self.brain.openai.ask.return_value = result if isinstance(result, str) else ""
            self.assertEqual((await self.reply(ev))[0], expected)

    async def test_rich_ai_reply_preserves_emotion_and_normal_chat_limit(self):
        ev = gift(value=200)
        extra = gift_thanks(ev)[1]
        self.cfg["live2d"]["enabled"] = True
        original = copy.deepcopy(self.cfg)
        for backend in ("openai", "qoder"):
            self.cfg["ai"]["backend"] = backend
            model = getattr(self.brain, backend).ask
            model.return_value = "【感谢】" + extra
            text, source, emotion = await self.reply(ev)
            self.assertEqual(source, "AI")
            self.assertEqual(emotion, "感谢")
            self.assertTrue(text.endswith(extra))
            self.assertGreater(len(extra), 40)
            call_cfg, system, prompt = model.await_args.args
            self.assertGreater(call_cfg["max_chars"], 40)
            self.assertIn(str(call_cfg["max_chars"]), system)
            self.assertIn("不要重复开头", prompt)
            self.assertIn("总价值约200元", prompt)
            self.assertEqual(self.cfg["ai"]["max_chars"], original["ai"]["max_chars"])

    async def test_disable_preserves_custom_template_and_ai_prompt(self):
        text, source, _ = await self.reply(gift(), tiered_thanks=False, mode="template",
                                          templates="收到{name}的礼物啦")
        self.assertEqual((text, source), ("收到小明的礼物啦", "模板"))
        self.brain.openai.ask.return_value = "谢谢你的支持！"
        text, source, _ = await self.reply(gift(), tiered_thanks=False, ai_prompt="感谢{name}")
        self.assertEqual((text, source), ("谢谢你的支持！", "AI"))
        self.assertEqual(self.brain.openai.ask.await_args.args[2].splitlines()[0], "感谢小明")

    def test_existing_configs_enable_feature_without_overwriting_custom_rules(self):
        cfg = merge(DEFAULTS, {"events": {"gift": {"templates": "原有文案"}}})
        self.assertTrue(cfg["events"]["gift"]["tiered_thanks"])
        self.assertEqual(cfg["events"]["gift"]["templates"], "原有文案")
        disabled = merge(DEFAULTS, {"events": {"gift": {"tiered_thanks": False}}})
        self.assertFalse(disabled["events"]["gift"]["tiered_thanks"])

    async def test_real_gift_packets_merge_before_thanks_and_reach_voice_queue(self):
        self.cfg["events"]["gift"]["mode"] = "template"
        app = SimpleNamespace(cfg=self.cfg, dt=None, brain=self.brain, pushes=[],
                              bili=SimpleNamespace(owner_uid=999), songs=Mock())
        app.push = lambda kind, data: app.pushes.append((kind, data))
        pipe = Pipeline(app)
        handler = _Handler(pipe.on_event)
        for _ in range(2):
            handler._on_gift(None, GiftMessage(uid=123, uname="小明", gift_name="小花花",
                                              num=10, total_coin=5000, coin_type="gold"))
        self.assertEqual(len(pipe.merging), 1)
        key, pending = pipe.merging.popitem()
        self.assertEqual(pending["ev"]["num"], 20)
        self.assertEqual(pending["ev"]["value"], 10)
        pipe._flush_merge(key, pending)
        task = asyncio.create_task(pipe._gen_worker())
        try:
            item = await asyncio.wait_for(pipe.synth_q.get(), 1)
            self.assertIn("小明老板送的20个小花花", item["text"])
            self.assertIn("比个心", item["text"])
            self.assertEqual(item["text"].count(GIFT_TOKEN_THANKS), 1)
            self.assertTrue(pipe.synth_q.empty())
            self.assertEqual(app.songs.on_gift.call_count, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_gift_filters_still_apply(self):
        app = SimpleNamespace(cfg=self.cfg, bili=SimpleNamespace(owner_uid=999), songs=Mock(), push=Mock())
        pipe = Pipeline(app)
        self.cfg["events"]["gift"]["thank_free"] = False
        pipe.on_event(gift(paid=False, value=0))
        self.assertFalse(pipe.merging)
        self.cfg["events"]["gift"]["enabled"] = False
        pipe.on_event(gift())
        self.assertFalse(pipe.merging)
        self.assertFalse(pipe.jobs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
