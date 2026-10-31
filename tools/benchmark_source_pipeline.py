r"""Compare local CPU frame stages with a pre-change git revision.

Example: .venv\Scripts\python.exe tools\benchmark_source_pipeline.py --baseline-ref HEAD
Excludes browser decode, native GDI presentation, audio, game and live encoder.
"""
import argparse
import ctypes
import json
import statistics
import subprocess
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

_DEFAULT_OPENCV_THREADS = cv2.getNumThreads()

import live_capture
import live_overlay
from live_source import SourceFrames


def load_revision(revision, filename):
    source = subprocess.check_output(["git", "show", f"{revision}:{filename}"], encoding="utf-8")
    module = types.ModuleType("baseline_" + filename.replace(".", "_"))
    exec(compile(source, filename, "exec"), module.__dict__)
    return module


def run(capture_class, compositor_class, native, animated, samples):
    rng = np.random.default_rng(42)
    source = np.empty((1080, 1920, 4), np.uint8)
    source[..., 0] = np.arange(1920, dtype=np.uint16)[None, :] % 256
    source[..., 1] = np.arange(1080, dtype=np.uint16)[:, None] % 256
    source[..., 2:] = 180
    sources = [source, source.copy()]
    sources[1][100:300, 300:500, :3] = (200, 120, 70)
    layers = []
    for _ in range(2):
        layer = np.zeros((720, 720, 4), np.uint8)
        layer[40:680, 160:560] = rng.integers(0, 256, (640, 400, 4), np.uint8)
        layers.append(layer)
    skin = types.SimpleNamespace(current=layers[0])
    overlay = types.SimpleNamespace(frame=lambda: skin.current)
    capture = capture_class()
    capture.compositor = compositor_class(overlay, lambda: ("br", 0.54, 0.02, 0.25, 0.6))
    reader = None
    if native:
        reader = SourceFrames(1920, 1080, capture.start_source_frames())
        pixels = ctypes.create_string_buffer(1920 * 1080 * 4)
    stages = {key: [] for key in ("capture_ms", "compose_and_handoff_ms", "jpeg_ms", "total_ms")}
    cpu_start = time.process_time()
    sequence = 0
    try:
        for i in range(samples + 10):
            if animated:
                skin.current = layers[i % 2]
            started = time.perf_counter()
            capture._on_frame(types.SimpleNamespace(frame_buffer=sources[i % 2]))
            captured = time.perf_counter()
            capture._compose_frame(capture._source_frame)
            if reader:
                sequence, _ = reader.copy_into(ctypes.addressof(pixels), sequence)
            composed = time.perf_counter()
            if not native:
                bgra = np.frombuffer(capture._frame, np.uint8).reshape(1080, 1920, 4)
                bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
                cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, live_capture.JPEG_QUALITY])
            finished = time.perf_counter()
            if i >= 10:
                for key, value in (("capture_ms", captured - started),
                                   ("compose_and_handoff_ms", composed - captured),
                                   ("jpeg_ms", finished - composed), ("total_ms", finished - started)):
                    stages[key].append(value * 1000)
        return {**{key: {"median": round(statistics.median(values), 3),
                         "p95": round(sorted(values)[int(len(values) * 0.95)], 3)}
                    for key, values in stages.items()},
                "cpu_ms_per_frame": round((time.process_time() - cpu_start) * 1000 / (samples + 10), 3)}
    finally:
        if reader:
            reader.close()
            capture.stop_source_frames()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--samples", type=int, default=120)
    parser.add_argument("--baseline-opencv-threads", type=int, default=_DEFAULT_OPENCV_THREADS)
    parser.add_argument("--output", type=Path, default=Path("outputs/source_performance.json"))
    args = parser.parse_args()
    if args.samples < 20:
        parser.error("--samples must be at least 20")
    old_capture = load_revision(args.baseline_ref, "live_capture.py")
    old_overlay = load_revision(args.baseline_ref, "live_overlay.py")
    optimized_threads = cv2.getNumThreads()
    result = {"baseline_commit": subprocess.check_output(["git", "rev-parse", args.baseline_ref],
                                                          encoding="utf-8").strip(),
              "resolution": [1920, 1080], "target_fps": 30,
              "baseline_opencv_threads": args.baseline_opencv_threads, "optimized_opencv_threads": optimized_threads,
              "samples": args.samples,
              "scope": "CPU capture callback, composite and handoff; excludes native GDI, browser decode, audio, game and live encoder"}
    for name, animated in (("reused_skin_frame", False), ("animated_skin", True)):
        cv2.setNumThreads(args.baseline_opencv_threads)
        before = run(old_capture.WindowStreamCapture, old_overlay.OverlayCompositor, False, animated, args.samples)
        cv2.setNumThreads(optimized_threads)
        after = run(live_capture.WindowStreamCapture, live_overlay.OverlayCompositor, True, animated, args.samples)
        result[name] = {"before": before, "after": after,
                        "median_reduction_percent": round((1 - after["total_ms"]["median"] /
                                                            before["total_ms"]["median"]) * 100, 1)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
