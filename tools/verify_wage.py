"""上班摸鱼离线回归：不联网、不调用模型/语音、不改真实配置或存档。"""
import asyncio
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from aiohttp.test_utils import TestClient, TestServer

import config
from live_broadcast import Broadcast
from live_wage import WageRenderer, demo_state
from wage import DEFAULTS, WageEngine, WagePlayer


class WageTests(unittest.TestCase):
    def setUp(self):
        self.settings = {**DEFAULTS, 'base_minutes': 25, 'auto_slack': False, 'catch_rate': 0,
                         'rescue_enabled': False, 'tasks_enabled': False, 'speed_decay_per_second': 0}
        self.engine = WageEngine(lambda: self.settings)
        self.now = 100.0
        self.engine._last_tick = self.now

    def advance(self, seconds):
        while seconds > 0:
            step = min(1, seconds)
            self.now += step
            self.engine.tick(self.now)
            seconds -= step

    def event(self, text):
        self.engine.on_event({'type': 'danmaku', 'uid': 42, 'name': '观众', 'text': text})

    def test_default_settings_match_config(self):
        self.assertEqual(DEFAULTS, config.DEFAULTS['wage'])

    def test_auto_slack_uses_bounded_one_to_three_minute_work_interval(self):
        for interval in (60, 180):
            with self.subTest(interval=interval):
                self.setUp()
                self.settings['auto_slack'] = True
                with patch('wage.random.uniform', return_value=interval) as sample:
                    self.advance(interval - .01)
                    self.assertEqual(self.engine.state['status'], 'working')
                    self.advance(.01)
                    self.assertEqual(self.engine.state['status'], 'slacking')
                    self.assertEqual(self.engine.take_cue()['type'], 'slack')
                    sample.assert_called_once_with(60, 180)

    def test_auto_slack_pause_and_state_reads_do_not_consume_interval(self):
        self.settings['auto_slack'] = True
        with patch('wage.random.uniform', return_value=60):
            self.advance(50)
            self.engine.set_paused(True)
            self.engine._last_tick = self.now
            self.advance(300)
            for i in range(20):
                self.engine.snapshot(self.now + i * 100)
            self.assertAlmostEqual(self.engine._auto_slack_left, 10)
            self.engine.set_paused(False)
            self.engine._last_tick = self.now
            self.advance(9)
            self.assertEqual(self.engine.state['status'], 'working')
            self.advance(1)
            self.assertEqual(self.engine.state['status'], 'slacking')

    def test_manual_slack_is_immediate_and_next_auto_interval_starts_after_slack(self):
        self.settings['auto_slack'] = True
        with patch('wage.random.uniform', side_effect=[180, 60]) as sample:
            self.advance(15)
            self.event('摸鱼')
            self.assertEqual(self.engine.state['status'], 'slacking')
            self.advance(self.engine._slack_total)
            self.assertEqual(self.engine.state['status'], 'working')
            self.advance(59)
            self.assertEqual(self.engine.state['status'], 'working')
            self.advance(1)
            self.assertEqual(self.engine.state['status'], 'slacking')
            self.assertEqual(sample.call_count, 2)

    def test_auto_slack_disabled_or_interval_change_resets_work_wait(self):
        with patch('wage.random.uniform', side_effect=lambda low, high: low) as sample:
            self.advance(180)
            sample.assert_not_called()
            self.settings['auto_slack'] = True
            self.advance(30)
            self.settings['slack_mean_seconds'] = 180
            self.advance(89)
            self.assertEqual(self.engine.state['status'], 'working')
            self.advance(1)
            self.assertEqual(self.engine.state['status'], 'slacking')
            sample.assert_any_call(90, 270)

    def test_work_countdown_settles_in_rounds_and_speed_accelerates(self):
        self.advance(24)
        self.assertEqual(self.engine.snapshot()['wage'], 0)
        self.assertAlmostEqual(self.engine.snapshot()['work_left'], 1)
        self.advance(1.1)
        self.assertEqual(self.engine.snapshot()['wage'], 100)
        self.engine.state['speed'] = 2
        self.advance(12.5)
        self.assertEqual(self.engine.snapshot()['wage'], 200)

    def test_slacking_discards_round_and_catch_can_make_wage_negative(self):
        self.advance(20)
        self.event('摸鱼')
        self.advance(4)
        self.assertEqual(self.engine.snapshot()['wage'], 0)
        self.event('老板来了')
        state = self.engine.snapshot()
        self.assertEqual((state['status'], state['white'], state['caught'], state['wage']), ('caught', 1, 1, -500))
        self.assertEqual(state['work_progress'], 0)
        self.advance(5)
        self.assertEqual(self.engine.snapshot()['status'], 'working')
        self.assertAlmostEqual(self.engine.snapshot()['focus_s'], 20)
        self.advance(25.1)
        self.assertEqual(self.engine.snapshot()['wage'], -400)

    def test_slack_timeout_only_whites_one_round_and_working_check_is_safe(self):
        self.event('老板来了')
        self.assertEqual(self.engine.snapshot()['caught'], 0)
        self.event('摸鱼')
        self.advance(self.engine._slack_total + 0.1)
        state = self.engine.snapshot()
        self.assertEqual((state['status'], state['white'], state['caught'], state['wage']), ('working', 1, 0, 0))
        self.event('老板来了')
        self.assertEqual(self.engine.snapshot()['caught'], 0)

    def test_fake_gifts_and_duplicate_platform_transactions(self):
        self.event('送出嘉年华 ×999')
        self.assertEqual(self.engine.snapshot()['wage'], 0)
        ev = {'type': 'gift', 'value': 10, 'paid': True, 'num': 10, 'gift_tid': 'transaction-1'}
        self.engine.on_event(ev)
        self.engine.on_event(ev)
        self.assertEqual(self.engine.snapshot()['wage'], 100)
        self.assertEqual(self.engine.snapshot()['speed'], 2)
        self.engine.on_event({'type': 'gift', 'value': 0, 'num': 3})
        self.assertEqual(self.engine.snapshot()['wage'], 115)
        self.engine.on_event({'type': 'superchat', 'price': float('nan')})
        self.assertTrue(np.isfinite(self.engine.snapshot()['wage']))

    def test_gift_reaches_target_once_and_offwork_progress_survives_restart(self):
        self.settings['target'] = 100
        self.engine.on_event({'type': 'gift', 'value': 20, 'paid': True})
        self.assertEqual(self.engine.snapshot()['status'], 'offwork')
        self.assertEqual(self.engine.snapshot()['wage'], 100)
        self.engine.on_event({'type': 'gift', 'value': 100, 'paid': True})
        self.assertEqual(self.engine.snapshot()['wage'], 100)
        with tempfile.TemporaryDirectory() as folder:
            self.engine.persist_path = str(Path(folder) / 'wage.json')
            self.engine.save()
            loaded = WageEngine(self.settings, persist_path=self.engine.persist_path)
            self.assertEqual(loaded.snapshot()['status'], 'offwork')
            self.assertEqual(loaded.snapshot()['next_day_left'], 45)
        self.advance(45.1)
        self.assertEqual(self.engine.snapshot()['status'], 'working')
        self.assertEqual(self.engine.snapshot()['wage'], 0)

    def test_pause_polling_and_preview_gaps_do_not_earn_wages(self):
        self.advance(10)
        before = self.engine.snapshot()
        for n in range(100):
            self.engine.snapshot(self.now + n)
        self.assertEqual(self.engine.snapshot()['work_progress'], before['work_progress'])
        self.engine.set_paused(True)
        self.event('摸鱼')
        self.advance(100)
        self.assertEqual(self.engine.snapshot()['work_progress'], before['work_progress'])
        self.assertEqual(self.engine.snapshot()['status'], 'working')
        self.engine.set_paused(False)
        self.engine._last_tick = self.now
        self.advance(15.1)
        self.assertEqual(self.engine.snapshot()['wage'], 100)
        self.engine._last_frame = self.now
        self.engine._renderer = SimpleNamespace(render=lambda *_a, **_kw: None)
        self.engine._renderer_room = self.engine.room_id
        progress = self.engine.snapshot()['work_progress']
        self.engine.frame(self.now + 600)
        self.assertEqual(self.engine.snapshot()['work_progress'], progress)

    def test_persistence_preserves_negative_wages_and_rejects_corrupt_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'wage.json'
            self.engine.persist_path = str(path)
            self.engine.state.update(wage=-500, work_progress=0.4)
            self.engine.save()
            loaded = WageEngine(self.settings, persist_path=path)
            self.assertEqual(loaded.snapshot()['wage'], -500)
            self.assertAlmostEqual(loaded.snapshot()['work_progress'], 0.4)
            raw = json.loads(path.read_text(encoding='utf8'))
            raw.update(white='broken', caught=None)
            path.write_text(json.dumps(raw), encoding='utf8')
            loaded = WageEngine(self.settings, persist_path=path)
            self.assertEqual(loaded.snapshot()['white'], 0)
            with patch.object(loaded, '_today', return_value='2099-01-02'):
                loaded.on_event({'type': 'danmaku', 'text': '你好'})
            self.assertEqual(loaded.snapshot()['wage'], 0)

    def test_speech_lifetime(self):
        with patch('wage.time.monotonic', return_value=100):
            self.engine.on_speak('speak', {'text': '今天准时下班'})
            self.engine.on_speak('speak_end', {})
        state = self.engine.snapshot(101)
        self.assertEqual(state['speech_until'], 106)
        renderer = WageRenderer()
        self.assertIsNotNone(renderer._speech_layer(state, 105))
        self.assertIsNone(renderer._speech_layer(state, 107))

    def test_poll_one_vote_per_viewer_result_and_pause(self):
        self.event('加薪投票')
        self.event('1')
        self.event('1')
        self.assertEqual(self.engine.snapshot()['poll']['yes'], 1)
        self.event('2')
        poll = self.engine.snapshot()['poll']
        self.assertEqual((poll['yes'], poll['no']), (0, 1))
        self.engine.set_paused(True)
        self.advance(50)
        self.assertEqual(self.engine.snapshot()['poll']['left'], 30)
        self.engine.set_paused(False)
        self.engine._last_tick = self.now
        self.advance(30)
        self.assertEqual(self.engine.snapshot()['poll']['result'], '继续打工')
        self.advance(6)
        self.assertIsNone(self.engine.snapshot()['poll'])
        self.event('加薪投票')
        self.assertIsNone(self.engine.snapshot()['poll'], '投票冷却期间不能重开')

    def test_renderer_states_and_bgra_foreground(self):
        renderer = WageRenderer()
        state = demo_state()
        outputs = []
        for status in ('working', 'slacking', 'caught', 'offwork'):
            state['status'] = status
            frame = renderer.render(state)
            self.assertEqual(frame.shape, (1080, 1920, 4))
            self.assertTrue((frame[..., 3] == 255).all())
            outputs.append(frame)
        self.assertFalse(np.array_equal(outputs[0], outputs[2]))
        # 鲨总必须在桌后；右下桌面不能再被它的裤腿或尾巴盖住。
        np.testing.assert_array_equal(outputs[0][1010:1045, 1690:1870],
                                      outputs[2][1010:1045, 1690:1870])
        state.update(status='working', poll={'left': 20, 'yes': 3, 'no': 2, 'result': ''})
        self.assertFalse(np.array_equal(outputs[0], renderer.render(state)))
        from PIL import Image
        canvas = np.zeros((4, 4, 4), np.uint8)
        canvas[..., 3] = 255
        renderer._blend(canvas, Image.new('RGBA', (2, 2), (255, 0, 0, 255)), 1, 1)
        self.assertEqual(canvas[1, 1].tolist(), [0, 0, 255, 255])
        self.assertTrue(renderer._wrap('很长的字幕' * 20, renderer.font(38), 200, 2)[-1].endswith('…'))

    def test_boss_gif_transparency_animation_and_pause(self):
        from PIL import Image
        for name in ('shark-boss-fullbody.gif', 'shark-boss-fullbody-idle.gif'):
            with Image.open(Path(config.WEB_DIR) / 'assets' / name) as gif:
                self.assertGreater(gif.n_frames, 20)
                self.assertEqual(gif.info['loop'], 0)
                self.assertEqual(gif.convert('RGBA').getchannel('A').getextrema(), (0, 255))
        renderer = WageRenderer()
        state = {**demo_state(), 'status': 'caught'}
        first = np.array(renderer._boss_layers(state, 100)[0][0])
        later = np.array(renderer._boss_layers(state, 100.66)[0][0])
        self.assertFalse(np.array_equal(first, later))
        state['paused'] = True
        paused = np.array(renderer._boss_layers(state, 102)[0][0])
        np.testing.assert_array_equal(paused, later)
        state.update(status='working', paused=False, poll={'left': 10, 'yes': 1, 'no': 0})
        renderer._boss_layers(state, 103)
        self.assertEqual(renderer._boss_mode, 'idle')


class PresenterTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_warmup_is_visible_and_clears_when_ready(self):
        app = SimpleNamespace(cfg={'tts': {'enabled': True}}, engine_ready=asyncio.Event(),
                              wage=WageEngine(DEFAULTS), push=Mock())
        player = WagePlayer(app, DEFAULTS)
        task = asyncio.create_task(player._wait_engine())
        await asyncio.sleep(0)
        self.assertIn('语音准备中', app.wage.snapshot()['presenter_status'])
        app.engine_ready.set()
        await task
        self.assertEqual(app.wage.snapshot()['presenter_status'], '')

    async def test_failed_line_is_visible_and_recovers_on_next_success(self):
        app = SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), wage=WageEngine(DEFAULTS),
                              brain=SimpleNamespace(ai=AsyncMock(side_effect=RuntimeError('offline'))),
                              pipe=Mock(), push=Mock())
        player = WagePlayer(app, DEFAULTS)
        self.assertFalse(await player._speak({'type': 'start'}, force=True))
        self.assertIn('台词生成失败', app.wage.snapshot()['presenter_status'])
        app.brain.ai = AsyncMock(return_value=('今天也要准时下班。', '开心'))
        self.assertTrue(await player._speak({'type': 'start'}, force=True))
        self.assertEqual(app.wage.snapshot()['presenter_status'], '')
        app.pipe.narrate_fresh.assert_called_once()

    def test_config_repairs_only_the_incorrect_fullbody_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / 'config.json'
            cases = [
                ({'avatar_x': .74, 'avatar_y': .57, 'avatar_size': .78, 'avatar_zoom': 1.0}, .52, 1.8),
                ({'avatar_x': .60, 'avatar_y': .65, 'avatar_size': .80, 'avatar_zoom': 2.1}, .60, 2.1),
                ({'avatar_x': .72, 'avatar_y': .67, 'avatar_size': .78, 'avatar_zoom': 1.8}, .52, 1.8),
            ]
            for layout, expected_x, expected_zoom in cases:
                config_path.write_text(json.dumps({'wage': layout}), encoding='utf8')
                with patch.object(config, 'CONFIG_PATH', str(config_path)):
                    actual = config.load()['wage']
                self.assertEqual(actual['avatar_x'], expected_x)
                self.assertEqual(actual['avatar_zoom'], expected_zoom)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_program_launcher_never_launches_or_stops_a_game(self):
        from dtgame.games import GameLauncher, ProgramSource
        launcher = object.__new__(GameLauncher)
        launcher.cfg = {'game': {'active': 'wage'}}
        launcher.demontower = Mock()
        launcher.program = ProgramSource()
        self.assertTrue(launcher.running())
        self.assertTrue((await launcher.launch_and_wait(Mock()))['program'])
        self.assertTrue(launcher.stop())
        self.assertEqual(launcher.audio_pids(), [])
        self.assertEqual(launcher.pid, 0)
        self.assertEqual(launcher.demontower.mock_calls, [])

    async def test_real_danmaku_worker_uses_wage_context_and_queues_speech(self):
        from pipeline import Pipeline
        app = SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), push=Mock(),
                              wage=WageEngine(DEFAULTS),
                              brain=SimpleNamespace(reply=AsyncMock(return_value=('鲨总还在盯着呢。', 'AI', '开心'))),
                              bili=SimpleNamespace(room_id=42))
        app.dt = WagePlayer(app, DEFAULTS)
        pipe = Pipeline(app, supporters=Mock())
        app.pipe = pipe
        ev = {'type': 'danmaku', 'name': '观众', 'uid': 42, 'text': '你工资多少了？', 'ts': time.time()}
        pipe._enqueue(ev)
        task = asyncio.create_task(pipe._gen_worker())
        try:
            item = await asyncio.wait_for(pipe.synth_q.get(), 2)
            self.assertEqual(item['text'], '鲨总还在盯着呢。')
            self.assertIn('今日', app.dt._prompt({'type': 'idle'}))
            self.assertIn('上班摸鱼', app.brain.reply.call_args.kwargs['context'])
            self.assertIn(item['text'], app.dt._recent)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import main
        self.app = object.__new__(main.App)
        self.app.cfg = copy.deepcopy(config.DEFAULTS)
        self.app.cfg['game']['active'] = 'wage'
        self.app.cfg['live2d']['enabled'] = False
        self.app.wage = WageEngine(lambda: self.app.cfg['wage'])
        self.app.bili = SimpleNamespace(client=None)
        self.app.dt = self.app.dt_task = None
        self.app.game = SimpleNamespace(sync=Mock())
        self.app.push = Mock()
        self.app.pipe = Mock()
        self.app.broadcast = Broadcast(self.app)
        self.app.broadcast.prepare_overlay = AsyncMock()
        self.client = TestClient(TestServer(main.build_web(self.app)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_settings_validation_status_and_preview_do_not_publish(self):
        headers = {'X-BVB': '1'}
        with patch('config.save') as save:
            response = await self.client.post('/api/config', json={'wage': {'work_reward': 200}}, headers=headers)
        self.assertEqual(response.status, 200)
        save.assert_called_once()
        for value in ({'target': 0}, {'work_reward': True}, {'catch_rate': float('nan')}, {'unknown': 1},
                      {'rescue_seconds': 2}, {'tasks_enabled': 'yes'}, {'rule_seconds': 121}):
            with patch('config.save') as save:
                response = await self.client.post('/api/config', json={'wage': value}, headers=headers)
                self.assertEqual(response.status, 400)
                save.assert_not_called()
        response = await self.client.get('/api/wage/preview.jpg')
        frame = cv2.imdecode(np.frombuffer(await response.read(), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(frame.shape, (1080, 1920, 3))
        status = await (await self.client.get('/api/dtgame/status')).json()
        self.assertEqual(status['game_kind'], 'wage')
        self.assertTrue(status['ready'])
        self.assertFalse(self.app.broadcast.running)
        self.assertFalse(self.app.broadcast.capture.previewing)

    async def test_program_preview_needs_audio_but_no_game_window(self):
        capture = self.app.broadcast.capture
        capture.start_program = Mock()
        response = await self.app.broadcast.preview({'audio_device': 0})
        self.assertTrue(response['ok'])
        callback, audio = capture.start_program.call_args.args
        self.assertEqual(audio, 0)
        self.assertEqual(callback().shape, (1080, 1920, 4))
        response = await self.app.broadcast.preview({})
        self.assertFalse(response['ok'])

    async def test_wage_schedule_selects_audio_only_and_checks_program_preview(self):
        from live_schedule import DailyBroadcastSchedule, _game_window
        schedule = DailyBroadcastSchedule(self.app)
        self.app.broadcast.sources = AsyncMock(return_value={
            'ok': True, 'windows': [], 'audio': [{'id': 7, 'name': '系统声音'}], 'default_audio': 7})
        selection = await schedule._plan_selection({'game': 'wage'})
        self.assertEqual(selection, {'hwnd': 0, 'audio_device': 7, 'program': True})
        capture = self.app.broadcast.capture
        capture.previewing, capture.program, capture.audio_device = True, Mock(), 7
        self.assertEqual(await schedule._source_names(selection), {'window_title': '', 'audio_name': '系统声音'})
        self.assertFalse(_game_window('wage', '任意游戏窗口'))
        # 默认工位半身，工作状态一并传到真实 Live2D 动作层。
        self.assertEqual(self.app.broadcast.overlay_render_settings()['zoom'], 1.8)
        self.assertEqual(self.app.broadcast.overlay_render_settings()['office']['status'], 'working')
        self.app.cfg['wage']['avatar_zoom'] = 1.0
        self.assertEqual(self.app.broadcast.overlay_render_settings()['zoom'], 1.0)

    async def test_switching_playback_stops_old_preview_and_rejects_live_switch(self):
        self.app.broadcast.capture.previewing = True
        self.app.broadcast.stop_preview = AsyncMock()
        headers = {'X-BVB': '1'}
        with patch('config.save'):
            response = await self.client.post('/api/config', json={'game': {'active': 'stardew'}}, headers=headers)
        self.assertEqual(response.status, 200)
        self.app.broadcast.stop_preview.assert_awaited_once()
        self.app.broadcast.phase = 'starting'
        with patch('config.save') as save:
            response = await self.client.post('/api/config', json={'game': {'active': 'wage'}}, headers=headers)
        self.assertEqual(response.status, 400)
        save.assert_not_called()
        self.assertEqual(self.app.cfg['game']['active'], 'stardew')


if __name__ == '__main__':
    unittest.main(verbosity=2)
