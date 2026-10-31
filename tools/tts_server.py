"""Local streaming Qwen3-TTS service; one model and one CUDA worker per process."""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging
import os
from pathlib import Path
import queue
import threading

import numpy as np
from aiohttp import web

log = logging.getLogger("tts-server")
ROOT = Path(__file__).resolve().parents[1]


class SpeechService:
    def __init__(self, args):
        self.args = args
        self.model = self.prompt = None
        self.error = ""
        self.phase = "loading"
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-cuda")
        self.lock = asyncio.Lock()
        self.load_task = None
        self.memory = {}
        self.torch = None

    def load(self):
        import torch
        from faster_qwen3_tts import FasterQwen3TTS
        if not torch.cuda.is_available():
            raise RuntimeError("流式语音需要 CUDA PyTorch，请安装 requirements-tts.txt 对应环境")
        path = self.args.model
        if path.startswith("Qwen/"):
            local = ROOT / "models" / "tts" / path.split("/")[-1]
            if (local / "model.safetensors").is_file() and (local / "speech_tokenizer" / "model.safetensors").is_file():
                path = str(local)
            else:
                from huggingface_hub import snapshot_download
                self.phase = "downloading"
                path = snapshot_download(path, local_dir=str(local),
                                         allow_patterns=["*.json", "*.safetensors", "*.txt", "*.tiktoken", "*.model"])
                self.phase = "loading"
        log.info("Loading speech model %s", path)
        with torch.inference_mode():
            self.model = FasterQwen3TTS.from_pretrained(path, device="cuda", dtype=torch.bfloat16,
                                                        attn_implementation="sdpa", max_seq_len=1024)
            self.prompt = self.model.model.create_voice_clone_prompt(
                ref_audio=self.args.ref_audio, x_vector_only_mode=True)
            self.model.warmup()
            # Warm up codec kernels as well as talker/predictor graphs, so the
            # first real utterance does not pay the lazy decoder setup cost.
            for _ in self.model.generate_voice_clone_streaming(
                    text="你好", language=self.args.language, voice_clone_prompt=self.prompt,
                    xvec_only=True, chunk_size=self.args.chunk_size, max_new_tokens=48):
                pass
        self.torch = torch
        self.phase = "ready"
        self.memory = {"vram_allocated_mib": round(torch.cuda.memory_allocated() / 1024**2, 1),
                       "vram_reserved_mib": round(torch.cuda.memory_reserved() / 1024**2, 1)}
        log.info("Speech model ready: %s", self.args.model)

    async def startup(self, app):
        async def load():
            try:
                await asyncio.get_running_loop().run_in_executor(self.executor, self.load)
            except Exception as error:
                self.phase, self.error = "error", str(error)
                log.exception("Speech model failed")
        self.load_task = asyncio.create_task(load())

    async def cleanup(self, app):
        if self.load_task:
            self.load_task.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)

    async def health(self, request):
        if self.phase == "ready" and self.torch is not None:
            self.memory = {"vram_allocated_mib": round(self.torch.cuda.memory_allocated() / 1024**2, 1),
                           "vram_reserved_mib": round(self.torch.cuda.memory_reserved() / 1024**2, 1)}
        return web.json_response({"ok": self.phase == "ready", "phase": self.phase,
                                  "model": self.args.model, "error": self.error,
                                  "service": "whaletalk-tts", "pid": os.getpid(), **self.memory})

    async def say(self, request):
        if self.phase != "ready":
            return web.json_response({"error": self.error or "语音模型正在加载"}, status=503)
        body = await request.json()
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            return web.json_response({"error": "语音文本需为 1 至 1000 字"}, status=400)
        chunks = queue.Queue(maxsize=4)
        cancelled = threading.Event()

        def put(value):
            while not cancelled.is_set():
                try:
                    chunks.put(value, timeout=0.1)
                    return True
                except queue.Full:
                    pass
            return False

        def generate():
            import torch
            try:
                with torch.inference_mode():
                    stream = self.model.generate_voice_clone_streaming(
                        text=text.strip(), language=str(body.get("language") or self.args.language),
                        voice_clone_prompt=self.prompt, xvec_only=True, chunk_size=self.args.chunk_size,
                        max_new_tokens=900)
                    try:
                        for audio, sr, timing in stream:
                            pcm = (np.clip(np.asarray(audio).reshape(-1), -1, 1) * 32767).astype("<i2").tobytes()
                            if not put((pcm, int(sr))):
                                break
                    finally:
                        stream.close()
            except Exception as error:
                log.exception("Speech synthesis failed")
                put(error)
            finally:
                put(None)

        async with self.lock:
            future = asyncio.get_running_loop().run_in_executor(self.executor, generate)
            response = None
            try:
                while True:
                    try:
                        item = await asyncio.to_thread(chunks.get, True, 0.25)
                    except queue.Empty:
                        if future.done():
                            break
                        continue
                    if isinstance(item, Exception):
                        if response is None:
                            return web.json_response({"error": str(item)}, status=500)
                        # Abort a partial stream so clients can distinguish a
                        # truncated utterance from a successful complete one.
                        if request.transport:
                            request.transport.close()
                        return response
                    if item is None:
                        break
                    pcm, rate = item
                    if response is None:
                        response = web.StreamResponse(headers={"Content-Type": "application/octet-stream",
                                                                "X-Sample-Rate": str(rate), "Cache-Control": "no-store"})
                        await response.prepare(request)
                    await response.write(pcm)
                if response is None:
                    return web.json_response({"error": "模型没有生成音频"}, status=500)
                await response.write_eof()
                return response
            finally:
                cancelled.set()
                # Keep the single-worker lease until CUDA generation finishes.
                await asyncio.shield(future)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="Qwen/Qwen3-TTS-12Hz-0.6B-Base")
    parser.add_argument("--ref-audio", required=True)
    parser.add_argument("--language", default="Chinese")
    parser.add_argument("--chunk-size", type=int, default=8)
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    if not Path(args.ref_audio).is_file() or not 1 <= args.chunk_size <= 32:
        parser.error("参考音频不存在，或流式块大小不在 1 至 32 之间")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = SpeechService(args)
    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/health", service.health)
    app.router.add_post("/say", service.say)
    app.on_startup.append(service.startup)
    app.on_cleanup.append(service.cleanup)
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
