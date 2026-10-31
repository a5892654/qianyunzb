"""Offline integration checks for the ocean stream skin; never starts a broadcast."""
import copy
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from aiohttp.test_utils import TestClient, TestServer

import config
from live_broadcast import Broadcast
from live_capture import OUT_H, OUT_W, WindowStreamCapture
from live_overlay import OverlayCompositor
from live_theme import DEFAULTS, GAME_RECT, RADIO_RECT, RADIO_SWITCH_SECONDS, StreamTheme, ThemeAvatar


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.settings = {**DEFAULTS, "preset": "ocean"}
        self.theme = StreamTheme(lambda: self.settings)
        self.game = np.full((540, 960, 4), (80, 60, 30, 255), np.uint8)

    def test_contains_all_edges_for_non_widescreen_games(self):
        x, y, w, h = GAME_RECT
        for shape in ((600, 800), (400, 1600), (800, 500)):
            with self.subTest(shape=shape):
                sh, sw = shape
                game = np.full((sh, sw, 4), (40, 70, 100, 255), np.uint8)
                game[:, :30] = (0, 0, 255, 255)
                game[:, -30:] = (0, 255, 0, 255)
                frame = self.theme.compose(game)
                self.assertEqual(frame.shape, (OUT_H, OUT_W, 4))
                self.assertTrue((frame[..., 3] == 255).all())
                roi = frame[y:y+h, x:x+w]
                self.assertTrue(np.any(np.all(roi == (0, 0, 255, 255), axis=2)), "left HUD edge cropped")
                self.assertTrue(np.any(np.all(roi == (0, 255, 0, 255), axis=2)), "right HUD edge cropped")

    def test_chat_is_live_expires_and_stays_behind_avatar(self):
        empty = self.theme.compose(self.game)
        self.theme.add_event({"type": "danmaku", "desc": "观众：今天也一起去冒险吧"})
        with patch('live_theme.time.monotonic', return_value=time.monotonic() + 0.5):
            chat = self.theme.compose(self.game)
        self.assertFalse(np.array_equal(empty[221:320, 42:414], chat[221:320, 42:414]))
        layer = np.full((100, 100, 4), (230, 50, 100, 255), np.uint8)
        compositor = OverlayCompositor(SimpleNamespace(frame=lambda: layer), lambda: ('bl', 0.2, 0, 0.12, 0.27))
        compositor.apply(chat)
        np.testing.assert_array_equal(chat[292, 230], layer[0, 0])
        self.settings['show_chat'] = False
        hidden = self.theme.compose(self.game)
        self.assertFalse(np.array_equal(hidden[221:320, 42:414], chat[221:320, 42:414]))
        self.settings['show_chat'] = True
        with patch('live_theme.time.monotonic', return_value=time.monotonic() + 121):
            self.theme.compose(self.game)
        with patch('live_theme.time.monotonic', return_value=time.monotonic() + 121.5):
            expired = self.theme.compose(self.game)
        np.testing.assert_array_equal(expired[221:700, 42:414], empty[221:700, 42:414])

    def test_chat_cards_slide_and_fade_on_new_messages(self):
        now = time.monotonic()
        with patch('live_theme.time.monotonic', return_value=now):
            self.theme.compose(self.game)
        self.theme.add_event({"type": "danmaku", "desc": "观众：第一条"})
        with patch('live_theme.time.monotonic', return_value=now + 0.05):
            start = self.theme.compose(self.game)
        with patch('live_theme.time.monotonic', return_value=now + 0.2):
            middle = self.theme.compose(self.game)
        with patch('live_theme.time.monotonic', return_value=now + 0.6):
            settled = self.theme.compose(self.game)
        with patch('live_theme.time.monotonic', return_value=now + 0.7):
            later = self.theme.compose(self.game)
        panel = (221, 700, 42, 414)
        self.assertFalse(np.array_equal(start[panel[0]:panel[1], panel[2]:panel[3]],
                                        middle[panel[0]:panel[1], panel[2]:panel[3]]))
        self.assertFalse(np.array_equal(middle[panel[0]:panel[1], panel[2]:panel[3]],
                                        settled[panel[0]:panel[1], panel[2]:panel[3]]))
        np.testing.assert_array_equal(settled[panel[0]:panel[1], panel[2]:panel[3]],
                                      later[panel[0]:panel[1], panel[2]:panel[3]])

    def test_moderation_bounded_history_and_clear(self):
        for reason in ('屏蔽词', '忽略名单'):
            self.theme.add_event({"type": "danmaku", "desc": "hidden", "skip": reason})
        self.assertEqual(len(self.theme._messages), 0)
        for i in range(90):
            self.theme.add_event({"type": "danmaku", "desc": f"观众：{i}", "skip": "冷却中"})
        self.assertEqual(len(self.theme._messages), 80)
        self.theme.clear()
        self.assertEqual(len(self.theme._messages), 0)

    def test_upper_body_crop_is_local_to_the_theme(self):
        layer = np.full((720, 720, 4), (80, 70, 60, 255), np.uint8)
        wrapped = ThemeAvatar(SimpleNamespace(frame=lambda: layer), self.theme)
        self.assertEqual(wrapped.frame().shape, (360, 360, 4))
        np.testing.assert_array_equal(wrapped.frame(), layer[47:407, 180:540])
        self.settings['preset'] = 'classic'
        self.assertIs(wrapped.frame(), layer)
        self.settings['preset'] = 'ocean'
        self.settings['avatar_zoom'] = 1
        np.testing.assert_array_equal(wrapped.frame(), layer)

    def test_switch_isolates_layout_and_shared_capture_frames(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['live2d'].update(enabled=True, overlay_x=0.8, overlay_y=0.7, overlay_size=0.4)
        original = dict(cfg['live2d'])
        app = SimpleNamespace(cfg=cfg)
        broadcast = Broadcast(app)
        classic = broadcast.overlay_settings()
        cfg['stream_theme']['preset'] = 'ocean'
        self.assertEqual(broadcast.overlay_settings(), ('bl', 0.54, 0, 0.125, 0.745))
        capture = broadcast.capture
        # A 4:3 source reaches the final game region without losing either side.
        source = np.full((600, 800, 4), (80, 70, 60, 255), np.uint8)
        source[:, :20] = (0, 0, 255, 255)
        source[:, -20:] = (0, 255, 0, 255)
        capture._on_frame(SimpleNamespace(frame_buffer=source))
        source[:] = 0  # capture owns the WGC callback buffer
        capture._compose_frame(capture._source_frame)
        pixels = np.frombuffer(capture._frame, np.uint8).reshape(OUT_H, OUT_W, 4)
        x, y, w, h = GAME_RECT
        self.assertTrue(np.any(np.all(pixels[y:y+h, x:x+w] == (0, 255, 0, 255), axis=2)))
        cfg['stream_theme']['avatar_x'] = 0.2
        cfg['stream_theme']['preset'] = 'classic'
        self.assertEqual(broadcast.overlay_settings(), classic)
        self.assertEqual(cfg['live2d'], original)
        capture._compose_frame(capture._source_frame)
        np.testing.assert_array_equal(np.frombuffer(capture._frame, np.uint8).reshape(OUT_H, OUT_W, 4), capture._source_frame)

    def test_missing_asset_falls_back_and_reports_error(self):
        capture = WindowStreamCapture()
        capture.theme_compositor = self.theme
        source = np.full((OUT_H, OUT_W, 4), 90, np.uint8)
        with patch('live_theme.BACKGROUND', Path('missing-theme-fixture.png')):
            capture._compose_frame(source)
        self.assertIn('主题合成失败', capture.snapshot()['theme_error'])
        self.assertEqual(capture._frame, source.tobytes())
        self.settings['preset'] = 'classic'
        capture._compose_frame(source)
        self.assertEqual(capture.snapshot()['theme_error'], '')

    def test_radio_bar_tracks_requests_progress_and_switches(self):
        x0, y0, x1, y1 = RADIO_RECT
        song = {'qid': 1, 'name': '海风来信', 'artist': '潮汐', 'by': '海盐汽水', 'source': 'request',
                'loading': False, 'paused': False, 'pos': 30.0, 'dur': 240.0}
        state = {'enabled': True, 'song': song}
        theme = StreamTheme(lambda: self.settings, lambda: state)
        idle = StreamTheme(lambda: self.settings).compose(self.game)
        playing = theme.compose(self.game)
        self.assertFalse(np.array_equal(idle[y0:y1, x0:x1], playing[y0:y1, x0:x1]))
        # 第一条观众点歌不算切换；换到第二首才亮出「已切换电台」。
        state['song'] = {**song, 'qid': 2, 'name': '第二首', 'pos': 0.0}
        now = time.monotonic()
        with patch('live_theme.time.monotonic', return_value=now):
            switched = theme.compose(self.game)
        with patch('live_theme.time.monotonic', return_value=now + RADIO_SWITCH_SECONDS + 0.1):
            settled = theme.compose(self.game)
        self.assertFalse(np.array_equal(switched[y0:y1, x0:x1], settled[y0:y1, x0:x1]))
        # 进度条随时长前进，缓存键必须区分播放位置。
        state['song'] = {**song, 'qid': 2, 'pos': 180.0}
        with patch('live_theme.time.monotonic', return_value=now + RADIO_SWITCH_SECONDS + 0.2):
            advanced = theme.compose(self.game)
        self.assertFalse(np.array_equal(settled[y0:y1, x0:x1], advanced[y0:y1, x0:x1]))

    def test_radio_bar_hides_song_request_ui_when_songs_disabled(self):
        hint = (1650, 995, 1860, 1025)  # right side of the bar, where the request hint lives
        enabled = StreamTheme(lambda: self.settings, lambda: {'enabled': True, 'song': None}).compose(self.game)
        disabled = StreamTheme(lambda: self.settings, lambda: {'enabled': False, 'song': None}).compose(self.game)
        self.assertFalse(np.array_equal(enabled[hint[1]:hint[3], hint[0]:hint[2]],
                                        disabled[hint[1]:hint[3], hint[0]:hint[2]]))
        self.assertGreater(np.count_nonzero(enabled[hint[1]:hint[3], hint[0]:hint[2]].max(axis=2) > 200), 100)
        broken = StreamTheme(lambda: self.settings, lambda: 1 / 0).compose(self.game)
        self.assertEqual(broken.shape, (OUT_H, OUT_W, 4))

    def test_broadcast_snapshot_feeds_radio_bar(self):
        from live_broadcast import song_snapshot
        app = SimpleNamespace(
            songs=SimpleNamespace(
                current={"qid": 3, "name": "晴天", "artist": "周杰伦", "by": "小小鲸鱼",
                         "source": "request", "dur": 269, "loading": False},
                cfg={"enabled": True}, paused=False),
            player=SimpleNamespace(music_progress=lambda: (61.0, 269.0)))
        snapshot = song_snapshot(app)
        self.assertTrue(snapshot["enabled"])
        self.assertEqual(snapshot["song"]["pos"], 61.0)
        self.assertEqual(snapshot["song"]["name"], "晴天")
        self.assertIsNone(song_snapshot(SimpleNamespace(cfg={})))

    def test_equalizer_animates_with_music_levels(self):
        x0, y0, x1, y1 = RADIO_RECT
        song = {'qid': 1, 'name': '海风来信', 'artist': '潮汐', 'by': '海盐汽水', 'source': 'request',
                'loading': False, 'paused': False, 'pos': 30.0, 'dur': 240.0, 'levels': [0.2] * 7}
        theme = StreamTheme(lambda: self.settings, lambda: {'enabled': True, 'song': dict(song)})
        now = time.monotonic()
        with patch('live_theme.time.monotonic', return_value=now):
            quiet = theme.compose(self.game)
        song['levels'] = [1.0] * 7
        with patch('live_theme.time.monotonic', return_value=now + 0.2):
            loud = theme.compose(self.game)
        song['levels'] = None
        with patch('live_theme.time.monotonic', return_value=now + 0.4):
            still = theme.compose(self.game)
        self.assertFalse(np.array_equal(quiet[y0:y1, x0:x1], loud[y0:y1, x0:x1]))
        self.assertFalse(np.array_equal(loud[y0:y1, x0:x1], still[y0:y1, x0:x1]))

    def test_music_levels_track_playing_audio(self):
        from tts import Player
        player = Player()
        self.assertIsNone(player.music_levels())
        sr = player.sr
        wave = (np.sin(2 * np.pi * 110 * np.arange(sr, dtype=np.float32) / sr) * 12000).astype(np.int16)
        player.music = np.stack([wave, wave], axis=1)
        player.mpos = sr // 2
        levels = player.music_levels()
        self.assertEqual(len(levels), 7)
        self.assertTrue(all(0 <= value <= 1 for value in levels))
        self.assertGreater(max(levels), 0.3)
        player.music_paused = True
        self.assertIsNone(player.music_levels())

    def test_chat_cards_distinguish_fans_guards_and_admin(self):
        def render(event):
            theme = StreamTheme(lambda: self.settings)
            theme.add_event(event)
            with patch('live_theme.time.monotonic', return_value=time.monotonic() + 0.5):
                frame = theme.compose(self.game)
            return frame[221:320, 42:414]

        plain = render({'type': 'danmaku', 'desc': '观众：你好'})
        variants = (
            {'medal': {'name': '深海团', 'level': 18, 'color': 0x2F6F8F}},
            {'guard': 3},
            {'admin': True},
            {'type': 'superchat', 'price': 30},
        )
        for extra in variants:
            with self.subTest(extra=extra):
                event = {'type': 'danmaku', 'desc': '观众：你好', **extra}
                self.assertFalse(np.array_equal(plain, render(event)))
        gift = {'type': 'gift', 'desc': '看海的人 送出 小花花 ×1（1元）', 'sender': '看海的人'}
        self.assertFalse(np.array_equal(render(gift), render({**gift, 'sender': ''})))
        from pipeline import badge
        info = badge({'type': 'danmaku', 'medal_name': '深海团', 'medal_level': 9, 'medal_color': 0x123456,
                      'guard_level': 3, 'admin': 1, 'name': '海盐汽水'})
        self.assertEqual(info['medal'], {'name': '深海团', 'level': 9, 'color': 0x123456})
        self.assertEqual(info['guard'], 3)
        self.assertTrue(info['admin'])
        self.assertEqual(info['sender'], '海盐汽水')

    def test_cached_composition_budget(self):
        self.theme.compose(self.game)
        times = []
        for _ in range(20):
            start = time.perf_counter()
            self.theme.compose(self.game)
            times.append(time.perf_counter() - start)
        print(f'Ocean composition: median {np.median(times)*1000:.1f} ms, p95 {np.percentile(times,95)*1000:.1f} ms')
        self.assertLess(np.median(times), 1 / 30, 'theme alone exceeded the 30 fps frame budget')


class ThemeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import main
        self.app = object.__new__(main.App)
        self.app.cfg = copy.deepcopy(config.DEFAULTS)
        self.app.bili = SimpleNamespace(client=None)
        self.app.broadcast = Broadcast(self.app)
        self.app.push = Mock()
        self.client = TestClient(TestServer(main.build_web(self.app)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_save_validation_and_local_preview(self):
        headers = {'X-BVB': '1'}
        with patch('config.save') as save:
            response = await self.client.post('/api/config', json={'stream_theme': {'preset': 'ocean', 'title': '海盐汽水'}}, headers=headers)
        self.assertEqual(response.status, 200)
        save.assert_called_once_with(self.app.cfg)
        saved = await (await self.client.get('/api/config')).json()
        self.assertEqual(saved['stream_theme']['preset'], 'ocean')
        self.assertEqual(saved['stream_theme']['title'], '海盐汽水')
        self.assertFalse(self.app.broadcast.capture.previewing)
        for patch_value in (None, {'preset': 'other'}, {'title': 'a'*25}, {'ticker': 'a'*301}, {'show_chat': 1}, {'avatar_x': True}, {'avatar_y': -1}, {'avatar_size': float('nan')}, {'unknown': 0}):
            with self.subTest(patch_value=patch_value), patch('config.save') as save:
                response = await self.client.post('/api/config', json={'stream_theme': patch_value}, headers=headers)
                self.assertEqual(response.status, 400)
                save.assert_not_called()
        response = await self.client.get('/api/theme/preview.jpg')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, 'image/jpeg')
        frame = cv2.imdecode(np.frombuffer(await response.read(), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(frame.shape, (1080, 1920, 3))
        # 预览里的电台条要带上点歌提示和示意曲目。
        hint = frame[995:1025, 1650:1860]
        self.assertGreater(np.count_nonzero(hint.max(axis=2) > 200), 100)
        self.assertEqual((await self.client.get('/static/assets/ocean-background.png')).status, 200)
        self.assertFalse(self.app.broadcast.running)


if __name__ == '__main__':
    unittest.main(verbosity=2)
