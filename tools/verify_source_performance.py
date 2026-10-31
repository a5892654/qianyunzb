"""Native source/IPC/JPEG demand regression. Local fixtures only; no platform API."""
import ctypes
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

from live_capture import OUT_H, OUT_W, WindowStreamCapture, _list_windows
from live_overlay import OverlayCompositor
from live_source import SourceFrames
from live_subtitles import SourceSubtitles


class SharedFramesTests(unittest.TestCase):
    def test_latest_frame_is_lossless_and_close_wakes_reader(self):
        writer = SourceFrames(32, 16)
        reader = SourceFrames(32, 16, writer.name)
        dest = ctypes.create_string_buffer(32 * 16 * 4)
        try:
            for value in range(4):
                self.assertTrue(writer.publish(bytes([value]) * len(dest)))
            self.assertTrue(reader.wait(20))
            self.assertEqual(reader.copy_into(ctypes.addressof(dest), 0), (4, False))
            self.assertEqual(dest.raw, bytes([3]) * len(dest))
            self.assertEqual(reader.owner_pid, writer.owner_pid)
            writer.close()
            self.assertTrue(reader.wait(20))
            self.assertEqual(reader.copy_into(ctypes.addressof(dest), 4), (4, True))
        finally:
            writer.close()
            reader.close()

    def test_busy_consumer_never_blocks_writer(self):
        writer = SourceFrames(8, 8)
        reader = SourceFrames(8, 8, writer.name)
        acquired, release = threading.Event(), threading.Event()

        def hold():
            from live_source import _kernel
            reader._acquire(1000)
            acquired.set()
            release.wait(2)
            _kernel.ReleaseMutex(reader.mutex)

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(acquired.wait(1))
            started = time.monotonic()
            self.assertFalse(writer.publish(bytes(256)))
            self.assertLess(time.monotonic() - started, 0.1)
        finally:
            release.set()
            thread.join(2)
            reader.close()
            writer.close()

    def test_missing_mapping_and_invalid_size_fail(self):
        with self.assertRaises(OSError):
            SourceFrames(8, 8, "Local\\missing-" + uuid.uuid4().hex)
        writer = SourceFrames(8, 8)
        try:
            with self.assertRaises(ValueError):
                writer.publish(b"short")
        finally:
            writer.close()


class RenderTests(unittest.TestCase):
    def test_no_jpeg_without_viewers_then_demand_and_expiry(self):
        capture = WindowStreamCapture()
        capture._on_frame(SimpleNamespace(frame_buffer=np.full((OUT_H, OUT_W, 4), 80, np.uint8)))
        capture.previewing = True
        worker = threading.Thread(target=capture._jpeg_worker)
        with patch("live_capture.cv2.imencode", wraps=cv2.imencode) as encode:
            worker.start()
            try:
                time.sleep(0.15)
                self.assertEqual(encode.call_count, 0)
                self.assertIsNotNone(capture._frame)
                initial = capture.wait_jpeg(0, 1)
                self.assertIsNotNone(initial)
                with capture._lock:
                    capture._jpeg_requested_until = 0
                calls = encode.call_count
                capture._on_frame(SimpleNamespace(frame_buffer=np.full((OUT_H, OUT_W, 4), 180, np.uint8)))
                time.sleep(0.15)
                self.assertEqual(encode.call_count, calls)
                self.assertEqual(capture._frame[0], 180)
                changed = capture.wait_jpeg(initial[0], 1)
                self.assertIsNotNone(changed)
                self.assertNotEqual(changed[1], initial[1])
            finally:
                capture._audio_stop.set()
                worker.join(2)

    def test_skin_frame_cache_and_clipping_match_float_blend(self):
        rng = np.random.default_rng(5)
        layer = rng.integers(0, 256, (100, 100, 4), np.uint8)
        settings = ["br", 0.5, 0, 0.05, 0.5]
        compositor = OverlayCompositor(SimpleNamespace(frame=lambda: layer), lambda: settings)
        with patch("live_overlay.cv2.resize", wraps=cv2.resize) as resize:
            for _ in range(2):
                frame = np.full((200, 400, 4), 80, np.uint8)
                compositor.apply(frame)
            self.assertEqual(resize.call_count, 0)  # already at output skin size
        src = layer[:, 30:, :3].astype(np.float32)
        alpha = layer[:, 30:, 3:4].astype(np.float32) / 255
        expected = (src * alpha + 80 * (1 - alpha)).astype(np.uint8)
        np.testing.assert_allclose(frame[50:150, :70, :3], expected, atol=1)
        self.assertTrue(np.all(frame[..., 3] == 80))

    def test_source_subtitles_preserve_legacy_frame_and_expire(self):
        settings = {"enabled": True, "seconds": 0}
        subtitles = SourceSubtitles(lambda: settings)
        capture = WindowStreamCapture()
        capture.source_subtitles = subtitles
        name = capture.start_source_frames()
        reader = SourceFrames(OUT_W, OUT_H, name)
        dest = ctypes.create_string_buffer(OUT_W * OUT_H * 4)
        source = np.full((OUT_H, OUT_W, 4), 80, np.uint8)
        try:
            subtitles.event("speak", {"text": "Native source subtitles 你好"})
            capture._compose_frame(source)
            seq, closed = reader.copy_into(ctypes.addressof(dest), 0)
            self.assertFalse(closed)
            self.assertEqual(capture._frame, source.tobytes())
            self.assertNotEqual(dest.raw, capture._frame)
            subtitles.event("speak_end", {})
            capture._compose_frame(source)
            seq2, _ = reader.copy_into(ctypes.addressof(dest), seq)
            self.assertGreater(seq2, seq)
            self.assertEqual(dest.raw, capture._frame)
            subtitles.event("speak", {"text": "Clear on disable"})
            subtitles.event("overlay_config", {"enabled": False})
            self.assertFalse(subtitles.apply(source.copy()))
        finally:
            reader.close()
            capture.stop_source_frames()


class NativeWindowTests(unittest.TestCase):
    def test_wgc_exact_1080p_updates_while_covered_and_closes(self):
        import windows_capture

        writer, cover = SourceFrames(OUT_W, OUT_H), SourceFrames(OUT_W, OUT_H)
        procs, frames, control = [], [], None

        def start(sink, suffix):
            title = "Qianyin QA " + suffix + uuid.uuid4().hex[:8]
            proc = subprocess.Popen([sys.executable, "tools/source_window.py", "http://127.0.0.1/source",
                                     title, str(OUT_W), str(OUT_H), "60", "60", "--shared-memory", sink.name],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            procs.append(proc)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                for window in _list_windows():
                    if window["title"] == title:
                        return window["hwnd"]
                if proc.poll() is not None:
                    self.fail("Native window exited during startup")
                time.sleep(0.03)
            self.fail("Native window did not appear")

        def wait_color(expected):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if frames and frames[0][1][:3] == expected:
                    self.assertEqual(frames[0][0], (OUT_H, OUT_W, 4))
                    return
                time.sleep(0.03)
            self.fail(f"Native WGC frame did not update: expected {expected}, got {frames}")

        try:
            hwnd = start(writer, "source-")
            capture = windows_capture.WindowsCapture(window_hwnd=hwnd, cursor_capture=False,
                                                     draw_border=False, minimum_update_interval=33)

            def on_frame_arrived(frame, capture_control):
                pixels = np.asarray(frame.frame_buffer)
                frames[:] = [(pixels.shape, tuple(int(v) for v in pixels[100, 100]))]

            def on_closed():
                pass

            capture.event(on_frame_arrived)
            capture.event(on_closed)
            control = capture.start_free_threaded()
            writer.publish(bytes((20, 40, 200, 255)) * (OUT_W * OUT_H))
            wait_color((20, 40, 200))
            start(cover, "cover-")
            cover.publish(bytes((70, 70, 70, 255)) * (OUT_W * OUT_H))
            time.sleep(0.1)
            writer.publish(bytes((200, 40, 20, 255)) * (OUT_W * OUT_H))
            wait_color((200, 40, 20))
        finally:
            if control:
                control.stop()
                control.wait()
            writer.close()
            cover.close()
            for proc in procs:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
