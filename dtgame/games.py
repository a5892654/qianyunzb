"""按 game.active 分派游戏启动器或不依赖游戏进程的节目源。"""

from __future__ import annotations

from dtgame.launcher import GameProcess
from dtgame.stardew import StardewProcess
from dtgame.sts2 import STS2Process


class ProgramSource:
    """节目画面由工具生成，启动/停止不能误操作游戏进程。"""

    proc = None

    def running(self):
        return True

    def launch(self):
        return {"ok": True, "program": True}

    async def launch_and_wait(self, check):
        return self.launch()

    def stop(self):
        return True

    def audio_pids(self):
        return []


class GameLauncher:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.demontower = GameProcess(cfg.get("dtgame", {}))
        self.stardew = StardewProcess(cfg.get("stardew", {}))
        self.sts2 = STS2Process(cfg.get("sts2", {}))
        self.program = ProgramSource()

    def sync(self, cfg: dict):
        self.cfg = cfg
        self.demontower.cfg = cfg.get("dtgame", {})
        self.stardew.cfg = cfg.get("stardew", {})
        self.sts2.cfg = cfg.get("sts2", {})

    @property
    def kind(self) -> str:
        return str((self.cfg.get("game") or {}).get("active", "demontower"))

    @property
    def is_stardew(self) -> bool:
        return self.kind == "stardew"

    @property
    def is_sts2(self) -> bool:
        return self.kind == "sts2"

    def current(self):
        if self.kind in ("wage", "climb"):
            return self.program
        if self.is_stardew:
            return self.stardew
        if self.is_sts2:
            return self.sts2
        return self.demontower

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
