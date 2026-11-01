"""送礼横幅离线验证：动画、队列、合成层与容错；不启动直播。"""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np

from config import DEFAULTS
from gift_banner import DURATION, GiftBanner
from live_capture import WindowStreamCapture
from pipeline import Pipeline


def gift(**overrides):
    return {"type": "gift", "uid": 123, "name": "小明", "gift": "小花花",
            "num": 1, "value": 1, "paid": True, **overrides}


def visible_columns(frame, baseline=60):
    changed = np.any(frame[..., :3] != baseline, axis=2)
    columns = np.where(changed.any(axis=0))[0]
    return (int(columns[0]), int(columns[-1])) if len(columns) else None


class GiftBannerTests(unittest.TestCase):
    def setUp(self):
        self.banner = GiftBanner()
        self.frame = np.full((1080, 1920, 4), 60, np.uint8)

    def trigger(self, at, **overrides):
        with patch("gift_banner.time.monotonic", return_value=at):
            return self.banner.trigger(gift(**overrides))

    def render(self, at):
        frame = self.frame.copy()
        with patch("gift_banner.time.monotonic", return_value=at):
            self.banner.apply(frame)
        return frame

    def test_banner_enters_from_right_holds_centered_and_leaves_left(self):
        self.assertTrue(self.trigger(0.0))
        self.assertIsNone(visible_columns(self.render(0.0)), "刚触发时横幅应在屏幕外")
        entry = visible_columns(self.render(0.1))
        hold = visible_columns(self.render(1.5))
        exit_ = visible_columns(self.render(2.9))
        self.assertIsNotNone(entry)
        self.assertIsNotNone(hold)
        self.assertIsNotNone(exit_)
        self.assertGreater(entry[0], hold[0], "横幅应从右往左进入")
        self.assertLess(exit_[1], hold[1], "横幅应从左离开")
        self.assertIsNone(visible_columns(self.render(DURATION + 0.1)), "三秒后必须消失")

    def test_each_gift_gets_its_turn_in_queue(self):
        self.trigger(0.0, name="小明", gift="小花花")
        self.trigger(0.0, name="小红", gift="人气票")
        self.assertEqual([item["name"] for item in self.banner._queue], ["小明", "小红"])
        self.assertIsNone(visible_columns(self.render(0.2)), "首个礼物从首帧开始进场")
        self.assertIsNotNone(visible_columns(self.render(0.3)))
        self.assertIsNotNone(visible_columns(self.render(2.5)))
        self.assertIsNone(visible_columns(self.render(3.3)), "第二个礼物重新从画面外进场")
        self.assertIsNotNone(visible_columns(self.render(3.4)), "第二个礼物应接续播放")
        self.assertIsNone(visible_columns(self.render(6.5)))

    def test_rapid_combo_from_same_donor_merges_into_one_card(self):
        for _ in range(3):
            self.trigger(0.0, num=2)
        self.assertEqual(len(self.banner._queue), 1)
        self.assertEqual(self.banner._queue[0]["num"], 6)
        self.trigger(1.0, name="小红")
        self.assertEqual(len(self.banner._queue), 2)
        self.trigger(1.5, name="小红", num=4)
        self.assertEqual(self.banner._queue[-1]["num"], 5)

    def test_stale_banners_do_not_pop_up_when_preview_starts_much_later(self):
        self.trigger(0.0)
        self.assertIsNone(visible_columns(self.render(20.0)))

    def test_invalid_gift_fields_are_safe(self):
        self.trigger(0.0, name="", gift="", num=True)
        item = self.banner._queue[0]
        self.assertEqual((item["name"], item["gift"], item["num"]), ("观众", "礼物", 1))
        for i, value in enumerate((None, 0, -3, "bad", 2.7)):
            self.trigger(i * 4.0, gift=f"礼物{i}", num=value)
        self.assertEqual([item["num"] for item in self.banner._queue], [1, 1, 1, 2])

    def test_render_cache_reuses_same_tile(self):
        self.trigger(0.0)
        self.render(1.0)
        keys = list(self.banner._tiles)
        self.render(1.2)
        self.assertEqual(list(self.banner._tiles), keys)
        self.assertEqual(len(self.banner._tiles), 1)

    def test_compositor_draws_banner_without_mutating_source(self):
        self.trigger(0.0)
        capture = WindowStreamCapture()
        capture.banner_compositor = self.banner
        source = self.frame.copy()
        with patch("gift_banner.time.monotonic", return_value=1.4):
            self.banner.apply(self.frame.copy())
        with patch("gift_banner.time.monotonic", return_value=1.5):
            capture._compose_frame(source)
        result = np.frombuffer(capture._frame, np.uint8).reshape(source.shape)
        self.assertIsNotNone(visible_columns(result))
        np.testing.assert_array_equal(source, self.frame)
        np.testing.assert_array_equal(result[:200], self.frame[:200])

    def test_render_failure_is_reported_and_next_gift_can_retry(self):
        self.trigger(0.0)
        with patch.object(GiftBanner, "_tile", side_effect=RuntimeError("fixture")):
            frame = self.frame.copy()
            with patch("gift_banner.time.monotonic", return_value=1.0):
                self.assertFalse(self.banner.apply(frame))
        self.assertIn("fixture", self.banner.error)
        np.testing.assert_array_equal(frame, self.frame)
        self.trigger(2.0)
        self.render(2.2)
        self.assertIsNotNone(visible_columns(self.render(2.3)))


class GiftBannerPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS)
        self.banner = GiftBanner()
        self.app = SimpleNamespace(cfg=self.cfg, dt=None, gift_banner=self.banner,
                                   bili=SimpleNamespace(owner_uid=999), songs=Mock(), pushes=[])
        self.app.push = lambda kind, data: self.app.pushes.append((kind, data))
        self.pipe = Pipeline(self.app, supporters=Mock())

    def test_gift_triggers_banner_after_merge_with_combined_count(self):
        with patch("gift_banner.time.monotonic", return_value=0.0):
            self.pipe.on_event(gift(num=2))
            self.pipe.on_event(gift(num=3))
        self.assertEqual(len(self.pipe.merging), 1)
        key, pending = self.pipe.merging.popitem()
        self.pipe._flush_merge(key, pending)
        self.assertEqual(len(self.banner._queue), 1)
        self.assertEqual(self.banner._queue[0]["num"], 5)
        self.assertEqual(self.banner._queue[0]["name"], "小明")

    def test_filtered_or_disabled_gifts_do_not_show_banner(self):
        self.cfg["events"]["gift"]["thank_free"] = False
        self.pipe.on_event(gift(paid=False, value=0))
        self.assertEqual(len(self.banner._queue), 0)
        self.cfg["events"]["gift"]["enabled"] = False
        self.pipe.on_event(gift())
        self.assertEqual(len(self.banner._queue), 0)

    def test_simulated_gift_still_previews_banner(self):
        self.pipe.on_event(gift(test=True, name="测试观众", gift="小花花", num=2))
        key, pending = self.pipe.merging.popitem()
        self.pipe._flush_merge(key, pending)
        self.assertEqual(self.banner._queue[0]["name"], "测试观众")


if __name__ == "__main__":
    unittest.main(verbosity=2)
