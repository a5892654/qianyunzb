r"""离线验证皮套透明合成、实时布局、预览与推流共用帧；不连接 B站。

运行：.venv\Scripts\python.exe tools\verify_live2d_overlay.py
"""
import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from aiohttp import web

from live_broadcast import Broadcast
from live_capture import OUT_H, OUT_W, WindowStreamCapture
from live_overlay import Live2DOverlay, OverlayCompositor, OverlayError, _decode_png, find_edge


class CompositorTests(unittest.TestCase):
    def setUp(self):
        self.layer = np.zeros((100, 100, 4), np.uint8)
        self.layer[10:90, 20:80] = (20, 60, 220, 255)
        self.overlay = SimpleNamespace(frame=lambda: self.layer)
        self.settings = ('br', 0.5, 0.02, 0.5, 0.5)
        self.compositor = OverlayCompositor(self.overlay, lambda: self.settings)

    def test_png_alpha(self):
        ok, png = cv2.imencode('.png', self.layer)
        self.assertTrue(ok)
        np.testing.assert_array_equal(_decode_png(png.tobytes()), self.layer)

    def test_position_size_and_dynamic_move(self):
        frame = np.zeros((200, 400, 4), np.uint8)
        self.compositor.apply(frame)
        self.assertEqual(int(frame[100, 200, 2]), 220)
        self.assertFalse(frame[100, 100].any())
        self.settings = ('br', 1.0, 0.02, 0.25, 0.5)
        moved = np.zeros_like(frame)
        self.compositor.apply(moved)
        self.assertEqual(int(moved[100, 100, 2]), 220)
        self.assertFalse(moved[100, 300].any())
        self.assertGreater(np.count_nonzero(moved[..., 2]), np.count_nonzero(frame[..., 2]))

    def test_edge_clips_without_moving_character(self):
        self.layer[:] = (10, 20, 200, 255)
        self.settings = ('br', 0.5, 0, 0, 0.5)
        frame = np.zeros((200, 400, 4), np.uint8)
        self.compositor.apply(frame)
        self.assertTrue(frame[100, 49, 2])
        self.assertFalse(frame[100, 50, 2])

    def test_alpha_blending_and_disabled(self):
        self.layer[:] = (200, 100, 0, 128)
        frame = np.full((200, 400, 4), 100, np.uint8)
        self.compositor.apply(frame)
        np.testing.assert_allclose(frame[100, 200, :3], (150, 100, 49), atol=1)
        self.settings = None
        off = np.full_like(frame, 100)
        self.compositor.apply(off)
        self.assertTrue(np.all(off == 100))

    def test_legacy_corner_settings(self):
        self.layer[:] = (0, 0, 200, 255)
        for corner, x, y in [('tl', 10, 10), ('tr', 390, 10), ('bl', 10, 190), ('br', 390, 190)]:
            self.settings = (corner, 0.5, 0.02)
            frame = np.zeros((200, 400, 4), np.uint8)
            self.compositor.apply(frame)
            self.assertTrue(frame[y, x, 2], corner)

    def test_preview_and_encoder_receive_same_composite(self):
        capture = WindowStreamCapture()
        capture.compositor = self.compositor
        source = np.full((OUT_H, OUT_W, 4), 80, np.uint8)
        capture._on_frame(SimpleNamespace(frame_buffer=source))
        capture._compose_frame(capture._source_frame)
        composite = capture._frame
        pixels = np.frombuffer(composite, np.uint8).reshape(OUT_H, OUT_W, 4)
        self.assertEqual(int(pixels[OUT_H // 2, OUT_W // 2, 2]), 220)
        encoded = []

        def write(data):
            encoded.append(data)
            capture._stop_evt.set()

        capture._proc = SimpleNamespace(stdin=SimpleNamespace(write=write, close=lambda: None))
        capture._pump_video()
        self.assertEqual(encoded, [composite])
        capture.previewing = True
        worker = threading.Thread(target=capture._jpeg_worker)
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while capture.frame_jpeg() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            jpg = cv2.imdecode(np.frombuffer(capture.frame_jpeg(), np.uint8), cv2.IMREAD_COLOR)
            np.testing.assert_allclose(jpg[OUT_H // 2, OUT_W // 2], pixels[OUT_H // 2, OUT_W // 2, :3], atol=5)
        finally:
            capture._audio_stop.set()
            worker.join(3)

    def test_static_game_keeps_refreshing_layout_without_ghosting(self):
        capture = WindowStreamCapture()
        capture.compositor = self.compositor
        capture._on_frame(SimpleNamespace(frame_buffer=np.full((OUT_H, OUT_W, 4), 80, np.uint8)))
        capture.previewing = True
        worker = threading.Thread(target=capture._jpeg_worker)
        worker.start()

        def wait_pixel(x, expected):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if capture._frame:
                    pixels = np.frombuffer(capture._frame, np.uint8).reshape(OUT_H, OUT_W, 4)
                    if pixels[OUT_H // 2, x, 2] == expected:
                        return pixels
                time.sleep(0.01)
            self.fail('静止游戏画面没有刷新皮套布局')

        try:
            wait_pixel(OUT_W // 2, 220)
            self.settings = ('br', 0.5, 0.02, 0.25, 0.5)
            moved = wait_pixel(OUT_W // 4, 220)
            self.assertEqual(int(moved[OUT_H // 2, OUT_W // 2, 2]), 80)
            self.settings = None
            wait_pixel(OUT_W // 4, 80)
        finally:
            capture._audio_stop.set()
            worker.join(3)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = SimpleNamespace(cfg={'live2d': {'enabled': False, 'overlay_in_broadcast': True}}, push=Mock())
        self.broadcast = Broadcast(self.app)
        self.broadcast.capture.previewing = True
        self.renderer = Mock(error='', alive=True)
        self.renderer.frame.return_value = np.ones((10, 10, 4), np.uint8)

    async def test_enable_after_preview_and_disable(self):
        await self.broadcast.refresh_overlay()
        self.assertIsNone(self.broadcast.capture.compositor)
        self.app.cfg['live2d'].update(enabled=True, model_file='model.model3.json')
        with patch('live_broadcast.find_edge', return_value='edge'), patch('live_broadcast.Live2DOverlay', return_value=self.renderer):
            await self.broadcast.refresh_overlay()
        self.renderer.start.assert_called_once()
        self.assertTrue(self.broadcast.status()['overlay'])
        self.app.cfg['live2d']['overlay_in_broadcast'] = False
        await self.broadcast.refresh_overlay()
        self.renderer.stop.assert_called_once()
        self.assertFalse(self.broadcast.status()['overlay'])

    async def test_failure_retained_and_no_platform_start(self):
        self.app.cfg['live2d'].update(enabled=True, model_file='model.model3.json')
        self.renderer.start.side_effect = OverlayError('模型渲染失败')
        with patch('live_broadcast.find_edge', return_value='edge'), patch('live_broadcast.Live2DOverlay', return_value=self.renderer):
            await self.broadcast.refresh_overlay()
        self.assertEqual(self.broadcast.status()['overlay_error'], '模型渲染失败')
        self.broadcast.capture.hwnd, self.broadcast.capture.audio_device = 1, 0
        with patch('live_broadcast.find_edge', return_value='edge'), patch('live_broadcast.Live2DOverlay', return_value=self.renderer), patch('live_broadcast.sources', return_value={'ffmpeg_ready': True}), patch('live_broadcast.BiliBroadcastPlatform') as platform:
            result = await self.broadcast.start({'hwnd': 1, 'audio_device': 0, 'confirm': True, 'title': '测试', 'area_id': 1})
        self.assertFalse(result['ok'])
        platform.assert_not_called()

    async def test_stale_frame_and_compositor_failure_are_not_ready(self):
        self.app.cfg['live2d'].update(enabled=True, model_file='model.model3.json')
        self.broadcast.overlay = self.renderer
        self.broadcast.capture.compositor = object()
        self.renderer.frame.return_value = None
        self.assertFalse(self.broadcast.status()['overlay'])
        self.broadcast.capture.compositor_error = '合成失败'
        self.assertEqual(self.broadcast.status()['overlay_error'], '合成失败')

    async def test_missing_model_is_visible(self):
        self.app.cfg['live2d']['enabled'] = True
        await self.broadcast.refresh_overlay()
        self.assertIn('模型文件', self.broadcast.status()['overlay_error'])


class BrowserCaptureTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(find_edge(), '本机没有 Edge')
    async def test_real_edge_transparent_frames_and_page_error(self):
        # 使用真实 Pixi WebGL 渲染器，合成图形替代仓库不分发的模型。
        root = Path(__file__).resolve().parents[1]
        html = (root / 'web/live2d.html').read_text(encoding='utf-8')
        html = html.replace('const qs =', '''PIXI.live2d.Live2DModel.from = async () => {
          const canvas = document.createElement('canvas'); canvas.width = 60; canvas.height = 90;
          const context = canvas.getContext('2d'); context.fillStyle = '#f05a82'; context.fillRect(0, 0, 60, 90);
          const sprite = new PIXI.Sprite(PIXI.Texture.from(canvas));
          sprite.internalModel = {};
          return sprite;
        };
        const qs =''')

        async def page(_):
            return web.Response(text=html, content_type='text/html')

        async def info(_):
            return web.json_response({'cfg': {'enabled': True, 'scale': 0.8, 'x': 0.5, 'y': 0},
                                      'expressions': [], 'motions': [], 'error': ''})

        async def error_page(_):
            return web.Response(text="<script>window.captureLive2DFrame=()=>({error:'fixture model failed'})</script>", content_type='text/html')

        app = web.Application()
        app.router.add_get('/', page)
        app.router.add_get('/api/l2d/info', info)
        app.router.add_static('/static/vendor/', root / 'web/vendor')
        app.router.add_get('/error', error_page)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        overlay = Live2DOverlay()
        try:
            await asyncio.to_thread(overlay.start, f'http://127.0.0.1:{port}/?overlay=1', 120)
            frame = overlay.frame()
            self.assertIsNotNone(frame)
            self.assertEqual(int(frame[0, 0, 3]), 0)
            self.assertEqual(int(frame[60, 60, 3]), 255)
            await asyncio.to_thread(overlay.stop)
            with self.assertRaisesRegex(OverlayError, 'fixture model failed'):
                await asyncio.to_thread(overlay.start, f'http://127.0.0.1:{port}/error', 120)
        finally:
            await asyncio.to_thread(overlay.stop)
            await runner.cleanup()


if __name__ == '__main__':
    unittest.main(verbosity=2)
