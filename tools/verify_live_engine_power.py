"""没开播时卸掉流式语音引擎：开播拉起、离线宽限期后释放、读不到状态不动。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import App

LIVE = {"ok": True, "logged_in": True, "live_status": 1}
OFF = {"ok": True, "logged_in": True, "live_status": 0}


def app(idle=False):
    a = object.__new__(App)
    a.engine_idle = idle
    a._engine_offline_since = None
    return a


class EnginePowerTests(unittest.TestCase):
    def test_live_starts_engine_when_idle(self):
        self.assertEqual(app(idle=True)._engine_power_action(LIVE, 1000.0), "start")

    def test_live_keeps_running_engine(self):
        self.assertEqual(app(idle=False)._engine_power_action(LIVE, 1000.0), "")

    def test_offline_unloads_after_grace(self):
        a = app()
        self.assertEqual(a._engine_power_action(OFF, 1000.0), "")
        self.assertEqual(a._engine_power_action(OFF, 1000.0 + App.IDLE_UNLOAD_SECONDS - 1), "")
        self.assertEqual(a._engine_power_action(OFF, 1000.0 + App.IDLE_UNLOAD_SECONDS), "unload")

    def test_live_resets_offline_timer(self):
        a = app()
        a._engine_power_action(OFF, 1000.0)
        self.assertEqual(a._engine_power_action(LIVE, 1300.0), "", "重新开播不能卸引擎")
        self.assertEqual(a._engine_power_action(OFF, 1400.0), "", "宽限期要从最后一次下播重新算")
        self.assertEqual(a._engine_power_action(OFF, 1400.0 + App.IDLE_UNLOAD_SECONDS), "unload")

    def test_unknown_status_never_unloads(self):
        a = app()
        for info in ({"ok": False, "error": "timeout"}, {"ok": True, "logged_in": False}):
            self.assertEqual(a._engine_power_action(info, 1000.0), "")
            self.assertEqual(a._engine_power_action(info, 9999.0), "")

    def test_already_idle_does_not_unload_again(self):
        a = app(idle=True)
        self.assertEqual(a._engine_power_action(OFF, 1000.0), "")
        self.assertEqual(a._engine_power_action(OFF, 9999.0), "")


if __name__ == "__main__":
    unittest.main()
