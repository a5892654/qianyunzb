"""鲨总音效离线验证：不打开声卡、不调用 TTS、不改变真实直播进度。"""
import asyncio
import copy
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from aiohttp.test_utils import TestClient, TestServer
import config
from tts import Player
from wage import WageEngine, DEFAULTS, validate_settings
from wage_audio import WageSfx, SOUNDS, load_sound


class EffectMixTests(unittest.TestCase):
    def player(self):
        player = Player()
        player.ensure = Mock()
        player._wait_done = Mock()
        player.sr = 1000
        return player

    def tick(self, player, count=100):
        result = np.zeros((count, 2), np.float32)
        player._callback(result, count, None, SimpleNamespace(output_underflow=False))
        return result

    def test_effect_is_resampled_stereo_and_does_not_drive_speech(self):
        player = self.player()
        player.play_effect(np.full((100, 1), 16384, np.int16), 2000, '直播设备', 50)
        out = self.tick(player)
        np.testing.assert_allclose(out[:50], .25)
        self.assertFalse(np.any(out[50:]))
        self.assertEqual(player.vheard, 0)
        self.assertFalse(player.playing)
        self.assertIsNone(player.effect)
        player.ensure.assert_called_once_with('直播设备')

    def test_speech_ducks_effect_without_changing_voice_clock_or_completion(self):
        player = self.player()
        player.play_blocking(np.full((400, 1), 8192, np.int16), 1000, '', 100)
        player.play_effect(np.full((400, 1), 16384, np.int16), 1000, '', 100)
        np.testing.assert_allclose(self.tick(player), .25 + .5 * .25)
        self.assertEqual(player.vheard, 100)
        self.assertFalse(player.voice_done.is_set())
        player.stop_effect()
        np.testing.assert_allclose(self.tick(player), .25)
        self.assertEqual(player.vheard, 200)
        self.assertFalse(player.voice_done.is_set())
        self.tick(player, 200)
        self.assertTrue(player.voice_done.is_set())

    def test_mixer_limits_peaks_and_replaces_instead_of_stacking(self):
        player = self.player()
        player.play_music(np.full((600, 2), 32767, np.int16), 1000, '', None)
        player.play_effect(np.full((5000, 1), 32767, np.int16), 1000, '', 100)
        self.assertEqual(len(player.effect), 3000)
        player.play_effect(np.full((400, 1), 16384, np.int16), 1000, '', 100)
        self.assertEqual(len(player.effect), 400)
        player.music_vol = 2
        self.assertLessEqual(np.abs(self.tick(player)).max(), 1)
        self.assertLess(player.mgain, 1)
        player.close()
        self.assertIsNone(player.effect)


class SoundRoutingTests(unittest.TestCase):
    def test_events_have_independent_ai_and_effect_queues_with_priority_and_expiry(self):
        engine = WageEngine(DEFAULTS)
        engine._cue('task_start')
        engine._cue('caught')
        engine._cue('promotion')
        self.assertEqual(engine.take_effect()['sound'], 'scold')
        self.assertEqual(engine.take_cue()['type'], 'promotion')
        self.assertIsNone(engine.take_effect())
        engine._cue('rules_start')
        engine._effects[0]['at'] -= 10
        self.assertIsNone(engine.take_effect())
        engine._cue('task_start')
        engine.set_paused(True)
        self.assertIsNone(engine.take_effect())

    def test_real_office_outcomes_route_to_appropriate_moods(self):
        engine = WageEngine(DEFAULTS)
        for pose, expected in [('approve', 'approve'), ('angry', 'angry')]:
            engine.play.pose = pose
            engine._cue('task_done')
            self.assertEqual(engine.take_effect()['sound'], expected)
        engine.state['status'] = 'caught'
        engine._cue('rescue_done')
        self.assertIsNone(engine.take_effect())
        engine.state['status'] = 'working'
        engine._cue('rescue_done')
        self.assertEqual(engine.take_effect()['sound'], 'approve')

    def test_assets_are_short_original_pcm_and_settings_validated(self):
        for name in SOUNDS:
            pcm, sr = load_sound(name)
            self.assertEqual(sr, 48000)
            self.assertEqual(pcm.dtype, np.int16)
            self.assertTrue(.5 < len(pcm) / sr <= 3)
            self.assertGreater(np.max(np.abs(pcm.astype(float))), 5000)
            self.assertLess(np.max(np.abs(pcm.astype(float))), 30000)
        for patch in ({'boss_sfx_volume': 101}, {'boss_sfx_volume': True},
                      {'boss_sfx_cooldown': float('nan')}, {'boss_sfx_enabled': 1}):
            with self.assertRaises(ValueError):
                validate_settings(patch)
        validate_settings({'boss_sfx_enabled': True, 'boss_sfx_volume': 45, 'boss_sfx_cooldown': 3})
        self.assertEqual(DEFAULTS, config.DEFAULTS['wage'])


class SoundControllerTests(unittest.IsolatedAsyncioTestCase):
    def app(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['audio']['device'] = '配音输出'
        app = SimpleNamespace(cfg=cfg, player=Mock(), push=Mock(),
                              game=SimpleNamespace(sync=Mock()), bili=SimpleNamespace(client=None),
                              _is_wage=Mock(return_value=True),
                              broadcast=SimpleNamespace(capture=SimpleNamespace(previewing=True)))
        app.wage = WageEngine(lambda: app.cfg['wage'])
        app.wage_sfx = WageSfx(app)
        return app

    async def test_events_cooldown_pause_disable_and_capture_stop(self):
        app = self.app()
        app.wage._cue('task_start')
        await app.wage_sfx.step()
        self.assertEqual(app.player.play_effect.call_count, 1)
        self.assertEqual(app.player.play_effect.call_args.args[2:], ('配音输出', 45))
        app.wage._cue('caught')
        await app.wage_sfx.step()
        self.assertEqual(app.player.play_effect.call_count, 1)
        app.wage_sfx.last_play -= 4
        app.wage._cue('caught')
        await app.wage_sfx.step()
        self.assertEqual(app.player.play_effect.call_count, 2)
        app.wage.set_paused(True)
        await app.wage_sfx.step()
        app.player.stop_effect.assert_called_once()
        app.wage.set_paused(False)
        for field in ('disabled', 'no_preview', 'other_game'):
            app.cfg['wage']['boss_sfx_enabled'] = field != 'disabled'
            app.broadcast.capture.previewing = field != 'no_preview'
            app._is_wage.return_value = field != 'other_game'
            app.wage_sfx.last_play -= 4
            app.wage._cue('task_start')
            await app.wage_sfx.step()
        self.assertEqual(app.player.play_effect.call_count, 2)
        self.assertIsNone(app.wage.take_effect())

    async def test_manual_audition_preserves_progress_and_is_not_stopped_by_idle_loop(self):
        app = self.app()
        app.broadcast.capture.previewing = False
        before = copy.deepcopy(app.wage.state)
        await app.wage_sfx.play('approve', manual=True)
        await app.wage_sfx.step()
        app.player.stop_effect.assert_not_called()
        self.assertEqual(before, app.wage.state)
        app.wage_sfx.audition_until = 0
        await app.wage_sfx.step()
        app.player.stop_effect.assert_called_once()

    async def test_audition_route_rejects_unknown_sound_and_preserves_progress(self):
        import main
        app = self.app()
        app.wage_sfx.play = AsyncMock()
        client = TestClient(TestServer(main.build_web(app)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.post('/api/wage/sfx/test', json={'sound': 'scold'}, headers={'X-BVB': '1'})
        self.assertEqual(response.status, 200)
        app.wage_sfx.play.assert_awaited_once_with('scold', manual=True)
        response = await client.post('/api/wage/sfx/test', json={'sound': '../bad'}, headers={'X-BVB': '1'})
        self.assertEqual(response.status, 400)
        response = await client.post('/api/wage/sfx/test', json={'sound': 'scold'})
        self.assertEqual(response.status, 403)

    async def test_master_switch_stops_audition_and_blocks_new_sound_until_enabled(self):
        app = self.app()
        await app.wage_sfx.play('scold', manual=True)
        self.assertGreater(app.wage_sfx.audition_until, time.monotonic())
        app.cfg['wage']['boss_sfx_enabled'] = False
        await app.wage_sfx.step()
        app.player.stop_effect.assert_called_once()
        self.assertEqual(app.wage_sfx.audition_until, 0)
        self.assertFalse(await app.wage_sfx.play('approve', manual=True))
        self.assertEqual(app.player.play_effect.call_count, 1)
        app.wage._cue('caught')
        await app.wage_sfx.step()
        app.cfg['wage']['boss_sfx_enabled'] = True
        app.wage_sfx.last_play -= 4
        await app.wage_sfx.step()
        self.assertEqual(app.player.play_effect.call_count, 1)
        app.wage._cue('caught')
        await app.wage_sfx.step()
        self.assertEqual(app.player.play_effect.call_count, 2)

    async def test_config_switch_is_saved_and_stops_sound_before_response(self):
        import main
        app = self.app()
        app.wage_sfx.stop = AsyncMock()
        client = TestClient(TestServer(main.build_web(app)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        with patch('config.save') as save:
            response = await client.post('/api/config', json={'wage': {'boss_sfx_enabled': False}}, headers={'X-BVB': '1'})
        self.assertEqual(response.status, 200)
        self.assertFalse(app.wage.settings()['boss_sfx_enabled'])
        app.wage_sfx.stop.assert_awaited_once()
        save.assert_called_once_with(app.cfg)


if __name__ == '__main__':
    unittest.main(verbosity=2)
