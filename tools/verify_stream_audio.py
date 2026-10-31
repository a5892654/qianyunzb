"""流式语音抗抖动回归：模拟音频到达和声卡回调，不播放声音、不调用模型。"""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from tts import Player


def pcm(frames):
    return np.full((frames, 1), 16384, np.int16)


class StreamingBufferTests(unittest.TestCase):
    def player(self, buffer=0.8, sr=1000):
        player = Player()
        player.sr = sr
        player.ensure = Mock()
        player._wait_done = Mock()
        player.begin_stream('', 100, buffer)
        return player

    def tick(self, player, frames=100, underflow=False):
        out = np.zeros((frames, 2), np.float32)
        player._callback(out, frames, None, SimpleNamespace(output_underflow=underflow))
        return out

    def test_prebuffer_absorbs_chunk_jitter_without_dropping_samples(self):
        def run(buffer):
            player = self.player(buffer)
            output = []
            for tick in range(14):
                if tick in (0, 6):
                    player.feed_stream(pcm(400), 1000)
                output.append(self.tick(player))
            return player, output

        immediate, old = run(0)
        buffered, new = run(0.8)
        self.assertTrue(np.any(old[0]))
        self.assertFalse(np.any(old[4]))
        self.assertEqual(immediate.stream_stats()['gaps'], 1)
        self.assertTrue(all(not np.any(chunk) for chunk in new[:6]))
        np.testing.assert_array_equal(np.concatenate(new[6:]), np.full((800, 2), 0.5, np.float32))
        self.assertEqual(buffered.vheard, 800)
        self.assertEqual(buffered.stream_stats()['gaps'], 0)
        buffered.end_stream()
        self.assertTrue(buffered.voice_done.is_set())

    def test_initial_buffer_does_not_advance_mouth_clock(self):
        player = self.player()
        player.feed_stream(pcm(300), 1000)
        self.assertFalse(np.any(self.tick(player)))
        self.assertEqual(player.voice_clock(), 0)
        self.assertEqual(player.vheard, 0)
        self.assertEqual(player.stream_stats()['gaps'], 0)

    def test_starvation_waits_for_refill_and_counts_one_gap_on_resume(self):
        player = self.player()
        player.feed_stream(pcm(800), 1000)
        for _ in range(8):
            self.assertTrue(np.any(self.tick(player)))
        self.assertFalse(np.any(self.tick(player)))
        self.assertFalse(np.any(self.tick(player)))
        player.feed_stream(pcm(300), 1000)
        self.assertFalse(np.any(self.tick(player)))
        self.assertEqual(player.vheard, 800)
        player.feed_stream(pcm(500), 1000)
        self.assertTrue(np.any(self.tick(player)))
        self.assertEqual(player.stream_stats(), {'gaps': 1, 'gap_ms': 300, 'output_underflows': 0})

    def test_short_sentence_and_short_final_chunk_are_flushed(self):
        for started in (False, True):
            with self.subTest(started=started):
                player = self.player()
                if started:
                    player.feed_stream(pcm(800), 1000)
                    self.tick(player, 800)
                    self.tick(player)
                player.feed_stream(pcm(150), 1000)
                self.assertFalse(np.any(self.tick(player)))
                player.end_stream()
                np.testing.assert_array_equal(self.tick(player, 150), np.full((150, 2), 0.5, np.float32))
                self.assertTrue(player.voice_done.is_set())

    def test_empty_stream_and_natural_end_are_not_reported_as_gaps(self):
        player = self.player()
        player.end_stream()
        self.assertTrue(player.voice_done.is_set())
        player.begin_stream('', 100, 0)
        player.feed_stream(pcm(50), 1000)
        self.tick(player)
        self.tick(player)
        player.end_stream()
        self.assertEqual(player.stream_stats()['gaps'], 0)

    def test_buffer_uses_output_sample_rate_and_music_continues_while_waiting(self):
        player = self.player(0.1, 48000)
        player.music = np.full((10000, 2), 16384, np.int16)
        player.feed_stream(pcm(1200), 24000)  # 重采样后 2400 帧，仍不足 4800。
        self.assertTrue(np.any(self.tick(player)))
        self.assertEqual(player.vheard, 0)
        self.assertEqual(player.mpos, 100)
        player.feed_stream(pcm(1200), 24000)
        self.tick(player)
        self.assertEqual(player.vheard, 100)

    def test_device_underflow_is_separate_and_stats_reset_for_next_sentence(self):
        player = self.player()
        self.tick(player, underflow=True)
        self.assertEqual(player.stream_stats(), {'gaps': 0, 'gap_ms': 0, 'output_underflows': 1})
        player.end_stream()
        player.begin_stream('', 100)
        self.assertEqual(player.stream_stats(), {'gaps': 0, 'gap_ms': 0, 'output_underflows': 0})

    def test_skip_discards_prebuffer_and_next_sentence_can_start(self):
        player = self.player()
        player.feed_stream(pcm(300), 1000)
        player.skip()
        self.assertFalse(np.any(self.tick(player)))
        self.assertTrue(player.voice_done.is_set())
        player.feed_stream(pcm(800), 1000)
        player.end_stream()
        self.assertFalse(np.any(self.tick(player)))
        player.begin_stream('', 100, 0)
        player.feed_stream(pcm(100), 1000)
        self.assertTrue(np.any(self.tick(player)))


class PipelineStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffer_setting_reaches_player_and_diagnostics_reach_log(self):
        from config import DEFAULTS
        from pipeline import Pipeline
        cfg = copy.deepcopy(DEFAULTS)
        cfg['tts']['stream_buffer_seconds'] = 1.2
        player = Mock()
        player.stream_stats.return_value = {'gaps': 2, 'gap_ms': 300, 'output_underflows': 1}
        app = SimpleNamespace(cfg=cfg, player=player, push=Mock())
        pipe = object.__new__(Pipeline)
        pipe.app = app
        async def chunks(*args):
            yield pcm(100), 1000
            yield pcm(200), 1000
        with patch('dtgame.ttsd.synth_stream', chunks):
            await pipe._play_stream({'id': 123, 'text': '缓冲测试'}, asyncio.get_running_loop())
        player.begin_stream.assert_called_once_with('', 130.0, 1.2)
        self.assertEqual(player.feed_stream.call_count, 2)
        player.end_stream.assert_called_once()
        warnings = [call.args[1]['msg'] for call in app.push.call_args_list if call.args[0] == 'log']
        self.assertTrue(any('语音缓冲耗尽 2 次' in message for message in warnings))
        self.assertTrue(any('音频设备输出欠载 1 次' in message for message in warnings))


class BufferSettingsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffer_setting_is_persisted_and_validated(self):
        import config
        import main
        from aiohttp.test_utils import TestClient, TestServer
        app = SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), bili=SimpleNamespace(client=None), push=Mock())
        client = TestClient(TestServer(main.build_web(app)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        for seconds in (0, 0.8, 1.2, 3):
            with self.subTest(seconds=seconds), patch('config.save') as save:
                response = await client.post('/api/config', json={'tts': {'stream_buffer_seconds': seconds}}, headers={'X-BVB': '1'})
                self.assertEqual(response.status, 200)
                save.assert_called_once_with(app.cfg)
                saved = await (await client.get('/api/config')).json()
                self.assertEqual(saved['tts']['stream_buffer_seconds'], seconds)
        for seconds in (-0.1, 3.1, True, '0.8', float('nan'), float('inf')):
            with self.subTest(seconds=seconds), patch('config.save') as save:
                response = await client.post('/api/config', json={'tts': {'stream_buffer_seconds': seconds}}, headers={'X-BVB': '1'})
                self.assertEqual(response.status, 400)
                save.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
