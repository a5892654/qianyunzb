"""摸鱼播放计划集成验证：真实调度/托管/音效状态，替换所有外部动作，不读写真实配置。"""
import asyncio
import copy
from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from live_schedule import DailyBroadcastSchedule
from main import App
from wage import WageEngine, WagePlayer
from wage_audio import WageSfx


class WagePlanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.save = patch('config.save', Mock())
        self.save.start()
        self.addCleanup(self.save.stop)
        # 运行真正的 Player.start/stop，但不让 AI/TTS/网络参与离线测试。
        async def idle(player):
            await asyncio.Event().wait()
        self.narration = patch.object(WagePlayer, 'run_forever', idle)
        self.narration.start()
        self.addCleanup(self.narration.stop)
        self.apps = []
        self.addAsyncCleanup(self.cleanup)

    async def cleanup(self):
        for app in self.apps:
            await app.stop_dt()
            await app.schedule.close()

    def fixture(self, mode='livehime', sound=True, active='demontower'):
        app = object.__new__(App)
        app.cfg = copy.deepcopy(config.DEFAULTS)
        app.cfg['game']['active'] = active
        app.cfg['wage']['boss_sfx_enabled'] = sound
        app.cfg['audio']['device'] = '测试输出'
        app.cfg['bili_cookie'] = ''
        app.cfg['broadcast']['auto_stop_minutes'] = 0
        app.cfg['broadcast']['auto_stop_line'] = ''
        plan = {'id': 'office', 'start': '20:00', 'stop': '02:00', 'game': 'wage', 'playlist': ''}
        app.cfg['broadcast']['schedule'].update(enabled=True, mode=mode, plans_enabled=True,
                                               plans=[plan], audio_name='', external_room_id=42,
                                               last_starts={}, last_stops={})
        app.wage = WageEngine(lambda: app.cfg['wage'])
        app.wage.set_paused(True)
        app.player, app.push = Mock(), Mock()
        app.dt = app.dt_task = None
        app.dt_resume_lock = asyncio.Lock()
        app.game = SimpleNamespace(sync=Mock(), launch_and_wait=AsyncMock())
        app.connect = AsyncMock()
        app.notify_live_started = Mock()
        app._task_died = Mock()
        app.engine_ready = asyncio.Event()
        app.engine_ready.set()
        capture = SimpleNamespace(previewing=False, program=None)
        br = SimpleNamespace(running=False, phase='idle', capture=capture, source_open=False)
        br.sources = AsyncMock(return_value={'ok': True, 'windows': [], 'default_audio': 7,
                                             'audio': [{'id': 7, 'name': '测试设备'}]})
        br.account = AsyncMock(return_value={'ok': True, 'logged_in': True, 'room_id': 42, 'live_status': 0})

        async def preview(selection):
            self.assertEqual(selection, {'hwnd': 0, 'audio_device': 7, 'program': True})
            capture.previewing, capture.program = True, app.wage.frame
            return {'ok': True}

        async def source(selection):
            await preview(selection)
            br.source_open = True
            return {'ok': True}

        async def start(selection):
            self.assertTrue(selection['confirm'])
            br.running, br.phase = True, 'live'
            return {'ok': True, 'room_id': 42}

        br.preview, br.start_source, br.start = AsyncMock(side_effect=preview), AsyncMock(side_effect=source), AsyncMock(side_effect=start)
        br.stop = AsyncMock(return_value={'ok': True})
        br.stop_preview = AsyncMock(return_value={'ok': True})
        br.stop_source = AsyncMock(return_value={'ok': True})
        br.stop_external = AsyncMock(return_value={'ok': True})
        br.sync_room_title = AsyncMock(return_value={'ok': True})
        app.broadcast = br
        app.livehime = SimpleNamespace(ensure_running=AsyncMock(return_value={'ok': True}),
                                      trigger=Mock(return_value={'ok': True}))
        app.wage_sfx = WageSfx(app)
        clock = [datetime(2026, 11, 12, 19, 59)]
        app.schedule = DailyBroadcastSchedule(app, clock=lambda: clock[0])
        app.schedule._wait_live_started = AsyncMock(return_value={'ok': True, 'logged_in': True, 'room_id': 42, 'live_status': 1})
        app.schedule._wait_live_stopped = AsyncMock(return_value=True)
        app.schedule.farewell_line = Mock(return_value='')
        self.apps.append(app)
        return app, clock

    async def fire(self, app, clock, moment):
        clock[0] = moment
        app.schedule.tick()
        while app.schedule.jobs:
            await asyncio.gather(*list(app.schedule.jobs))
        await asyncio.sleep(0)

    async def test_livehime_plan_starts_real_wage_and_stops_next_day_with_sound(self):
        app, clock = self.fixture()
        await self.fire(app, clock, datetime(2026, 11, 12, 20, 0))
        self.assertTrue(app.schedule.last_result['ok'], app.schedule.last_result)
        self.assertEqual(app.cfg['game']['active'], 'wage')
        self.assertIsInstance(app.dt, WagePlayer)
        self.assertFalse(app.wage.snapshot()['paused'])
        app.game.launch_and_wait.assert_not_awaited()
        app.broadcast.start_source.assert_awaited_once()
        app.broadcast.start.assert_not_awaited()
        app.livehime.trigger.assert_called_once_with('start')
        self.assertEqual(app.broadcast.capture.program().shape, (1080, 1920, 4))
        app.wage._cue('task_start')
        await app.wage_sfx.step()
        self.assertEqual(app.player.play_effect.call_args.args[2:], ('测试输出', 45))
        app.broadcast.account.return_value['live_status'] = 1
        await self.fire(app, clock, datetime(2026, 11, 13, 2, 0))
        self.assertTrue(app.schedule.last_result['ok'], app.schedule.last_result)
        self.assertTrue(app.wage.snapshot()['paused'])
        self.assertIsNone(app.dt)
        app.broadcast.stop_external.assert_awaited_once_with(42)
        # 源画面窗口保留时，下播后也不能继续响。
        self.assertTrue(app.broadcast.capture.previewing)
        await app.wage_sfx.step()
        app.player.stop_effect.assert_called_once()
        await self.fire(app, clock, datetime(2026, 11, 13, 20, 0))
        self.assertFalse(app.wage.snapshot()['paused'])

    async def test_saved_sound_off_survives_plan_switch_and_restart(self):
        app, clock = self.fixture(sound=False)
        await self.fire(app, clock, datetime(2026, 11, 12, 20, 0))
        self.assertTrue(app.schedule.last_result['ok'], app.schedule.last_result)
        self.assertFalse(app.cfg['wage']['boss_sfx_enabled'])
        app.wage._cue('caught')
        await app.wage_sfx.step()
        app.player.play_effect.assert_not_called()
        app.cfg['wage']['boss_sfx_enabled'] = True
        await app.wage_sfx.step()
        app.player.play_effect.assert_not_called()
        app.wage._cue('caught')
        await app.wage_sfx.step()
        app.player.play_effect.assert_called_once()

    async def test_managed_plan_uses_program_preview_without_game_window(self):
        app, clock = self.fixture(mode='managed', active='wage')
        await self.fire(app, clock, datetime(2026, 11, 12, 20, 0))
        self.assertTrue(app.schedule.last_result['ok'], app.schedule.last_result)
        app.broadcast.preview.assert_awaited_once()
        self.assertEqual(app.broadcast.start.call_args.args[0], {'hwnd': 0, 'audio_device': 7, 'program': True, 'confirm': True})
        self.assertFalse(app.wage.snapshot()['paused'])
        await self.fire(app, clock, datetime(2026, 11, 13, 2, 0))
        self.assertTrue(app.wage.snapshot()['paused'])
        app.broadcast.stop.assert_awaited_once()

    async def test_plan_stop_pauses_wage_even_when_bili_status_check_fails(self):
        app, clock = self.fixture()
        await self.fire(app, clock, datetime(2026, 11, 12, 20, 0))
        self.assertTrue(app.schedule.last_result['ok'], app.schedule.last_result)
        self.assertFalse(app.wage.snapshot()['paused'])
        self.assertTrue(app.broadcast.source_open)
        # 停播时 B站状态查询失败：本地摸鱼仍要暂停，直播姬仍要收到停止键，不能把节目晾着继续跑。
        app.broadcast.account.return_value = {'ok': False, 'error': 'B站请求失败，请稍后重试'}
        app.livehime.trigger.reset_mock()
        await self.fire(app, clock, datetime(2026, 11, 13, 2, 0))
        self.assertTrue(app.wage.snapshot()['paused'])
        self.assertIsNone(app.dt)
        app.livehime.trigger.assert_called_once_with('stop')
        app.broadcast.stop_external.assert_not_awaited()
        self.assertFalse(app.schedule.last_result['ok'])
        self.assertIn('B站请求失败', app.schedule.last_result['message'])

    async def test_livehime_account_glitch_opens_source_then_pauses_fresh_wage(self):
        # 线上事故回归（19:00 wage/livehime「B站请求失败」后整晚空说）：account 持续失败时
        # 重试都在同一次执行内完成；源画面必须已启动；本次拉起的托管失败后必须暂停。
        app, clock = self.fixture()
        app.broadcast.account = AsyncMock(return_value={'ok': False, 'error': 'B站请求失败，请稍后重试'})
        app.schedule.ACCOUNT_TRIES = 3
        app.schedule.ACCOUNT_RETRY_SECONDS = 0
        await self.fire(app, clock, datetime(2026, 11, 12, 20, 0))
        self.assertFalse(app.schedule.last_result['ok'])
        self.assertIn('B站请求失败', app.schedule.last_result['message'])
        self.assertEqual(app.broadcast.account.await_count, 3)  # 只重试读取，恰好 ACCOUNT_TRIES 次
        app.broadcast.start_source.assert_awaited_once()
        self.assertTrue(app.broadcast.source_open)  # 源画面已保留
        app.livehime.trigger.assert_not_called()  # 状态没确认绝不发开播快捷键
        # 本次自动启动的摸鱼托管被暂停：不能再整晚空说
        self.assertTrue(app.wage.snapshot()['paused'])
        self.assertIsNone(app.dt)
        # 收尾不销毁源画面、不停直播间、不动 B站停播接口
        app.broadcast.stop_source.assert_not_awaited()
        app.broadcast.stop_external.assert_not_awaited()
        # 账本仍是「已触发」去重语义：当天同分钟不再补开
        self.assertEqual(app.cfg['broadcast']['schedule']['last_starts']['office'], '2026-11-12 20:00')
        app.broadcast.account.reset_mock()
        await self.fire(app, clock, datetime(2026, 11, 12, 20, 5))
        app.broadcast.account.assert_not_awaited()
        app.broadcast.start_source.assert_awaited_once()

    async def test_livehime_source_failure_makes_zero_platform_calls(self):
        # 源画面起不来 → 立即失败，一个 B站请求都不发；刚启动的托管同样收尾暂停
        app, clock = self.fixture()
        app.broadcast.start_source = AsyncMock(return_value={'ok': False, 'error': '源画面窗口启动失败：进程退出'})
        app.schedule.ACCOUNT_TRIES = 2
        app.schedule.ACCOUNT_RETRY_SECONDS = 0
        await self.fire(app, clock, datetime(2026, 11, 12, 20, 0))
        self.assertFalse(app.schedule.last_result['ok'])
        self.assertIn('源画面', app.schedule.last_result['message'])
        app.broadcast.account.assert_not_awaited()
        app.livehime.trigger.assert_not_called()
        self.assertTrue(app.wage.snapshot()['paused'])
        self.assertIsNone(app.dt)

    async def test_livehime_failure_keeps_pre_existing_manual_wage(self):
        # 开播前用户已手动开了摸鱼托管：失败收尾只清「这次启动的」，手动托管原样保留
        app, clock = self.fixture(active='wage')
        self.assertTrue((await app.resume_dt()).get('ok'))
        app.broadcast.account = AsyncMock(return_value={'ok': False, 'error': 'B站请求失败，请稍后重试'})
        app.schedule.ACCOUNT_TRIES = 2
        app.schedule.ACCOUNT_RETRY_SECONDS = 0
        await self.fire(app, clock, datetime(2026, 11, 12, 20, 0))
        self.assertFalse(app.schedule.last_result['ok'])
        app.broadcast.start_source.assert_awaited_once()
        self.assertIsNotNone(app.dt)
        self.assertFalse(app.wage.snapshot()['paused'])


    async def test_stop_releases_tts_and_next_start_reboots(self):
        app, clock = self.fixture()
        app.engine = Mock()
        app.engine.start = AsyncMock(return_value=True)
        app._engine_booting = False
        app.cfg['tts'].update(enabled=True, qwen_enabled=True, stream_enabled=True, auto_start=True)
        await self.fire(app, clock, datetime(2026, 11, 12, 20, 0))
        self.assertTrue(app.schedule.last_result['ok'], app.schedule.last_result)
        app.engine.stop.reset_mock()
        app.broadcast.account.return_value['live_status'] = 1
        await self.fire(app, clock, datetime(2026, 11, 13, 2, 0))
        self.assertTrue(app.schedule.last_result['ok'], app.schedule.last_result)
        # 下播释放流式语音引擎显存（保留 ComfyUI），并清掉就绪标记。
        app.engine.stop.assert_called_once_with(keep_comfyui=True)
        self.assertFalse(app.engine_ready.is_set())
        # 下一段开播：托管启动自动重新热身语音引擎。
        app.engine.start.reset_mock()
        await self.fire(app, clock, datetime(2026, 11, 13, 20, 0))
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertTrue(app.schedule.last_result['ok'], app.schedule.last_result)
        app.engine.start.assert_awaited()
        self.assertTrue(app.engine_ready.is_set())


if __name__ == '__main__':
    unittest.main(verbosity=2)
