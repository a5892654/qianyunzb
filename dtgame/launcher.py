"""启动/管理 Godot 游戏进程。

不需要用户先去编辑器按 F5 —— 鲸语自己把游戏拉起来，
并且把 PID 绑到主程序，退出时一起收掉，不留孤儿进程。
"""

import asyncio
import logging
import os
import shutil
import subprocess

import procutil

log = logging.getLogger("dtlaunch")

# 常见安装位置，按顺序找
_CANDIDATES = [
    r"C:\rg\Godot_v4.7.2-stable_win64.exe",
    r"C:\Program Files\Godot\Godot.exe",
    r"C:\Program Files (x86)\Godot\Godot.exe",
]


def find_godot(cfg_path: str = "") -> str:
    """定位 Godot 可执行文件：配置优先，其次常见路径，最后 PATH。"""
    if cfg_path and os.path.exists(cfg_path):
        return cfg_path
    for p in _CANDIDATES:
        if os.path.exists(p):
            return p
    for name in ("godot.exe", "godot4.exe", "Godot_v4.7-stable_win64.exe"):
        hit = shutil.which(name)
        if hit:
            return hit
    import glob
    for pattern in (r"C:\rg\Godot*.exe", r"C:\Godot*.exe",
                    r"C:\Program Files\**\Godot*.exe", r"C:\Program Files (x86)\**\Godot*.exe"):
        hits = sorted(glob.glob(pattern, recursive=True))
        if hits:
            return hits[-1]
    return ""


class GameProcess:
    """把游戏当子进程管起来，带就绪探测。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None

    @property
    def project_dir(self) -> str:
        return str(self.cfg.get("project_dir", "")).rstrip("\\/")

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def launch(self) -> dict:
        if self.running():
            return {"ok": True, "already": True, "pid": self.proc.pid}
        godot = find_godot(str(self.cfg.get("godot_exe", "")))
        if not godot:
            return {"ok": False, "error": "找不到 Godot 可执行文件，请在设置里指定 godot.exe 路径"}
        if not self.project_dir or not os.path.exists(os.path.join(self.project_dir, "project.godot")):
            return {"ok": False, "error": f"项目目录无效：{self.project_dir}"}
        args = [godot, "--path", self.project_dir]
        if self.cfg.get("headless"):
            args.insert(1, "--headless")
        try:
            self.proc = subprocess.Popen(
                args, cwd=self.project_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=procutil.CREATE_NO_WINDOW if self.cfg.get("headless") else 0,
            )
        except OSError as e:
            return {"ok": False, "error": f"启动失败：{e}"}
        procutil.bind_to_app(self.proc.pid)
        log.info("游戏已启动 pid=%s", self.proc.pid)
        return {"ok": True, "pid": self.proc.pid, "godot": godot}

    async def launch_and_wait(self, bridge_check, timeout: float = 90.0) -> dict:
        """启动游戏并等桥接端口就绪。bridge_check 是个无参可调用，就绪返回 True。"""
        r = self.launch()
        if not r.get("ok"):
            return r
        if r.get("already") and await asyncio.to_thread(bridge_check):
            return r
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await asyncio.to_thread(bridge_check):
                return {"ok": True, "pid": self.proc.pid if self.proc else 0}
            if not self.running():
                return {"ok": False, "error": "游戏进程退出了，检查项目是否能在本机正常启动"}
            await asyncio.sleep(0.5)
        return {"ok": False, "error": f"游戏启动超时（{int(timeout)}秒），桥接端口没起来"}

    def stop(self) -> bool:
        if not self.running():
            return False
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None
        return True
