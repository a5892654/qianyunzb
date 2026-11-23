"""帧龄看门狗：游戏窗口被最小化时自动还原，其它原因造成的冻结只告警、绝不重启采集。"""
import sys
import threading
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
    cap.program = None
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


class ProgramReviveTests(unittest.TestCase):
    """程序源（上班摸鱼等）生产线程出错退出后，只重开这一条线程，不动音频和窗口。"""

    def program_capture(self, latest=1000.0):
        cap = object.__new__(WindowStreamCapture)
        cap.previewing = True
        cap.program = lambda: None
        cap.latest = latest
        cap._program_thread = None
        cap._program_revive_at = 0.0
        cap._program_failures = 0
        cap._program_gave_up = False
        cap.error = "程序画面生成失败: boom"
        cap._lock = threading.Lock()
        return cap

    @staticmethod
    def dead_thread():
        t = threading.Thread(target=lambda: None, name="dead-program")
        t.start()
        t.join()
        return t

    def test_dead_program_thread_is_revived(self):
        cap = self.program_capture()
        cap._program_thread = self.dead_thread()
        with patch.object(live_capture.time, "monotonic", return_value=1000.0):
            cap._revive_program()
        self.assertEqual(cap._program_failures, 1)
        self.assertEqual(cap.error, "")
        self.assertIsNotNone(cap._program_thread)
        cap._program_thread.join(timeout=2)

    def test_revive_has_cooldown(self):
        cap = self.program_capture()
        cap._program_thread = self.dead_thread()
        with patch.object(live_capture.time, "monotonic", return_value=1000.0):
            cap._revive_program()
            cap._program_thread.join(timeout=2)
            cap._revive_program()
        self.assertEqual(cap._program_failures, 1)

    def test_fresh_frames_reset_failures(self):
        cap = self.program_capture(latest=999.5)
        stop = threading.Event()
        cap._program_thread = threading.Thread(target=stop.wait, args=(5,), daemon=True)
        cap._program_thread.start()
        cap._program_failures = 3
        with patch.object(live_capture.time, "monotonic", return_value=1000.0):
            cap._revive_program()
        stop.set()
        self.assertEqual(cap._program_failures, 0)

    def test_gives_up_after_five_revives(self):
        cap = self.program_capture()
        cap._program_thread = self.dead_thread()
        for i in range(6):
            with patch.object(live_capture.time, "monotonic", return_value=1000.0 + i * 11.0):
                cap._revive_program()
            if cap._program_thread is not None:
                cap._program_thread.join(timeout=2)
        self.assertEqual(cap._program_failures, 5)
        self.assertTrue(cap._program_gave_up)


if __name__ == "__main__":
    unittest.main()
