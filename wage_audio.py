"""鲨总事件音效：短音效、单通道、冷却、主播说话时自动避让。"""
from __future__ import annotations

import asyncio
import logging
import time
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np
from config import WEB_DIR

log = logging.getLogger('wage_audio')
SOUNDS = ('inspect', 'scold', 'assign', 'angry', 'approve')


@lru_cache(maxsize=5)
def load_sound(name):
    if name not in SOUNDS:
        raise ValueError('未知鲨总音效')
    with wave.open(str(Path(WEB_DIR) / 'assets' / 'wage-sfx' / f'{name}.wav')) as source:
        if source.getsampwidth() != 2 or source.getnchannels() != 1:
            raise ValueError('鲨总音效必须是单声道 16 位 WAV')
        sr = source.getframerate()
        pcm = np.frombuffer(source.readframes(min(source.getnframes(), sr * 3)), '<i2').reshape(-1, 1).copy()
    return pcm, sr


class WageSfx:
    def __init__(self, app):
        self.app = app
        self.last_play = float('-inf')
        self.audition_until = 0
        self._lock = asyncio.Lock()
        self._last_warning = float('-inf')
        self._was_active = False

    async def play(self, name, manual=False):
        async with self._lock:
            settings = self.app.wage.settings()
            volume = float(settings['boss_sfx_volume'])
            if not settings['boss_sfx_enabled'] or volume <= 0:
                return False
            pcm, sr = load_sound(name)
            await asyncio.to_thread(self.app.player.play_effect, pcm, sr,
                                    self.app.cfg.get('audio', {}).get('device', ''), volume)
            self.last_play = time.monotonic()
            if manual:
                self.audition_until = self.last_play + len(pcm) / sr + .3
            return True

    async def stop(self):
        async with self._lock:
            self.audition_until = 0
            self._was_active = False
            self.app.wage.take_effect()
            self.app.player.stop_effect()

    async def step(self):
        cue = self.app.wage.take_effect()
        settings = self.app.wage.settings()
        if not settings['boss_sfx_enabled']:
            if self._was_active or self.audition_until:
                await self.stop()
            return
        now = time.monotonic()
        active = (self.app._is_wage() and self.app.broadcast.capture.previewing
                  and settings['enabled'] and settings['boss_sfx_enabled']
                  and settings['boss_sfx_volume'] > 0 and not self.app.wage.snapshot()['paused'])
        if not active:
            if now >= self.audition_until and self._was_active:
                self.app.player.stop_effect()
            self._was_active = now < self.audition_until
            return
        self._was_active = True
        if cue and now - self.last_play >= float(settings['boss_sfx_cooldown']):
            await self.play(cue['sound'])

    async def run(self):
        try:
            while True:
                try:
                    await self.step()
                except Exception:
                    if time.monotonic() - self._last_warning >= 30:
                        log.warning('鲨总音效播放失败', exc_info=True)
                        self._last_warning = time.monotonic()
                await asyncio.sleep(.1)
        finally:
            self.app.player.stop_effect()
