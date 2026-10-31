"""按 game.active 分派启动器：恶魔之塔（Godot）或星露谷（SMAPI）。"""

from __future__ import annotations

from dtgame.launcher import GameProcess
from dtgame.stardew import StardewProcess


class GameLauncher:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.demontower = GameProcess(cfg.get("dtgame", {}))
        self.stardew = StardewProcess(cfg.get("stardew", {}))

    def sync(self, cfg: dict):
        self.cfg = cfg
        self.demontower.cfg = cfg.get("dtgame", {})
        self.stardew.cfg = cfg.get("stardew", {})

    @property
    def kind(self) -> str:
        return str((self.cfg.get("game") or {}).get("active", "demontower"))

    @property
    def is_stardew(self) -> bool:
        return self.kind == "stardew"

    def current(self):
        return self.stardew if self.is_stardew else self.demontower

    def running(self) -> bool:
        return self.current().running()

    def launch(self) -> dict:
        return self.current().launch()

    async def launch_and_wait(self, check) -> dict:
        return await self.current().launch_and_wait(check)

    def stop(self) -> bool:
        return self.current().stop()

    @property
    def proc(self):
        return getattr(self.current(), "proc", None)

    @property
    def pid(self) -> int:
        proc = self.proc
        return proc.pid if proc is not None and proc.poll() is None else 0

    def audio_pids(self) -> list[int]:
        current = self.current()
        hook = getattr(current, "audio_pids", None)
        if callable(hook):
            try:
                return hook()
            except Exception:
                return []
        proc = getattr(current, "proc", None)
        return [proc.pid] if proc is not None and proc.poll() is None else []
