"""Run a real 0.6B streaming smoke test using an explicitly supplied test reference.

Never modifies application settings or plays audio on the speaker.
"""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
import config
from dtgame import ttsd


async def run(reference):
    root = Path(config.BASE_DIR)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = {**config.DEFAULTS["tts"], "qwen_ref_audio": str(reference.resolve()),
                "ttsd_base": f"http://127.0.0.1:{port}",
                "ttsd_python": str(root / ".tts-venv/Scripts/python.exe"),
                "ttsd_script": str(root / "tools/tts_server.py")}
    started = time.monotonic()
    try:
        if not await ttsd.ensure(settings, lambda message: print(message, flush=True)):
            raise RuntimeError(ttsd.tail_log(55))
        load_seconds = time.monotonic() - started
        results = []
        for index in range(2):
            chunks, first = [], None
            started = time.monotonic()
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.post(settings["ttsd_base"] + "/say", json={"text": "你好，这是零点六B模型的流式语音测试。", "language": "Chinese"},
                                        timeout=aiohttp.ClientTimeout(total=120)) as response:
                    if response.status != 200:
                        raise RuntimeError(await response.text())
                    rate = int(response.headers["X-Sample-Rate"])
                    async for chunk in response.content.iter_any():
                        if chunk and first is None:
                            first = time.monotonic() - started
                        chunks.append(chunk)
            elapsed = time.monotonic() - started
            pcm = b"".join(chunks)
            assert len(pcm) > rate and len(pcm) % 2 == 0
            with wave.open(str(root / f"outputs/tts_06b_test_{index}.wav"), "wb") as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate); output.writeframes(pcm)
            results.append({"first_chunk_s": round(first, 3), "generation_s": round(elapsed, 3),
                            "audio_s": round(len(pcm) / 2 / rate, 3), "sample_rate": rate})
        result = {"load_s": round(load_seconds, 2), "health": await ttsd.health(settings), "utterances": results,
                  "reference": "Windows SAPI test fixture, not the user's voice"}
        (root / "outputs/tts_06b_runtime.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        await asyncio.to_thread(ttsd.stop)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref-audio", type=Path, required=True)
    args = parser.parse_args()
    if not args.ref_audio.is_file():
        parser.error("Reference file not found")
    asyncio.run(run(args.ref_audio))
