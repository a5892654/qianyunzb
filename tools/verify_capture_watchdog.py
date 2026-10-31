"""帧龄看门狗：游戏窗口被最小化时自动还原，其它原因造成的冻结只告警、绝不重启采集。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_capture
from live_capture import WindowStreamCapture


def capture(previewing=True, hwnd=1234, latest=100.0):
    cap = object.__new__(WindowStreamCapture)
    cap.previewing = previewing
    cap.hwnd = hwnd
    cap.latest = latest
    cap._revived_at = 0.0
    return cap


class ReviveTests(unittest.TestCase):
    def test_fresh_frames_do_nothing(self):
        cap = capture()
        with patch.object(live_capture, "_is_minimized", side_effect=AssertionError("不该查最小化")):
            self.assertEqual(cap.revive_if_stale(now=101.0), {})

    def test_minimized_game_is_restored_without_activation(self):
        cap = capture()
        calls = []
        with patch.object(live_capture, "_is_minimized", return_value=True), \
                patch.object(live_capture, "_show_noactivate", lambda h: calls.append(h)):
            info = cap.revive_if_stale(now=105.0)
        self.assertTrue(info.get("revived"))
        self.assertEqual(calls, [1234])

    def test_restore_has_cooldown(self):
        cap = capture()
        calls = []
        with patch.object(live_capture, "_is_minimized", return_value=True), \
                patch.object(live_capture, "_show_noactivate", lambda h: calls.append(h)):
            cap.revive_if_stale(now=105.0)
            cap.revive_if_stale(now=106.0)   # 冷却期内不重复捅窗口
            cap.revive_if_stale(now=112.0)   # 过了冷却再来一次
        self.assertEqual(calls, [1234, 1234])

    def test_visible_but_frozen_only_reports(self):
        cap = capture()
        with patch.object(live_capture, "_is_minimized", return_value=False), \
                patch.object(live_capture, "_show_noactivate", side_effect=AssertionError("不该动窗口")):
            info = cap.revive_if_stale(now=108.0)
        self.assertTrue(info.get("stale"))
        self.assertAlmostEqual(info["age"], 8.0)

    def test_never_touches_inactive_capture(self):
        for cap in (capture(previewing=False), capture(hwnd=0), capture(latest=0.0)):
            with patch.object(live_capture, "_is_minimized", side_effect=AssertionError("不该查")):
                self.assertEqual(cap.revive_if_stale(now=500.0), {})


if __name__ == "__main__":
    unittest.main()
