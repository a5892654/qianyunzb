import argparse
import atexit
import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

APP_TITLE = "千音ai直播工具"
OCCUPIED_MSG = "千音ai直播工具端口被其他程序或旧版服务占用，请先正常退出原程序。"


def resolve_root(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-dir", default="")
    args, _ = parser.parse_known_args(argv)
    if args.project_dir:
        return Path(args.project_dir).resolve()
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


class DesktopController:
    def __init__(self, project_root):
        self.root = Path(project_root)
        cfg = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.port = int(cfg.get("web_port", 8787))
        self.base = f"http://127.0.0.1:{self.port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method, path, payload=None, timeout=6.0):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"X-BVB": "1", "Content-Type": "application/json"})
        with self.opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def status(self):
        try:
            info = self._request("GET", "/api/desktop/status")
        except (urllib.error.HTTPError, ValueError):
            return "occupied", None
        except (urllib.error.URLError, OSError):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return "occupied", None
            except OSError:
                return "down", None
        return ("ok", info) if isinstance(info, dict) and info.get("app") == "WhaleTalk" else ("occupied", None)

    def start_service(self):
        python = self.root / ".venv" / "Scripts" / "python.exe"
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        with (self.root / "logs" / "desktop_service.log").open("ab") as logfile:
            return subprocess.Popen([str(python), "main.py", "--no-ui", "--manual-dt"],
                                    cwd=str(self.root), env=env, stdout=logfile, stderr=logfile,
                                    creationflags=0x08000000)

    def wait_online(self, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state, info = self.status()
            if state == "ok":
                return info
            if state == "occupied":
                raise RuntimeError(OCCUPIED_MSG)
            time.sleep(0.25)
        raise RuntimeError("千音ai直播工具启动超时，请查看 logs/desktop_service.log。")

    def ensure_service(self):
        state, info = self.status()
        if state == "occupied":
            raise RuntimeError(OCCUPIED_MSG)
        if state == "down":
            self.start_service()
            info = self.wait_online()
        return info

    def resume(self, new_run=False):
        return self._request("POST", "/api/dtgame/resume", {"new_run": new_run}, timeout=110.0)

    def stop(self):
        return self._request("POST", "/api/dtgame/stop", {}, timeout=10.0)

    def quit(self):
        return self._request("POST", "/api/quit", {}, timeout=5.0)

    def wait_closed(self, timeout=130.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status()[0] == "down":
                return True
            time.sleep(0.25)
        return False


class SingleInstance:
    def __init__(self, project_root):
        digest = hashlib.sha1(str(project_root).lower().encode("utf-8")).hexdigest()[:16]
        self.name = "Local\\WhaleTalkWebApp-" + digest
        self.handle = None

    def acquire(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = kernel.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            self.release()
            return False
        atexit.register(self.release)
        return True

    def release(self):
        if self.handle:
            kernel = ctypes.WinDLL("kernel32")
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(self.handle)
            self.handle = None


def focus_existing_window():
    user = ctypes.windll.user32
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def focus(handle, _):
        title = ctypes.create_unicode_buffer(200)
        user.GetWindowTextW(handle, title, len(title))
        if user.IsWindowVisible(handle) and title.value == APP_TITLE:
            user.ShowWindow(handle, 9)
            user.SetForegroundWindow(handle)
            return False
        return True
    user.EnumWindows(callback(focus), 0)


class DesktopApi:
    def __init__(self, app):
        self._app = app

    def close(self):
        return self._app.close()


class WebApp:
    def __init__(self, project_root):
        self.ctrl = DesktopController(project_root)
        self.window = None
        self.shutdown_lock = threading.Lock()
        self.stopped = False

    def shutdown(self):
        with self.shutdown_lock:
            if self.stopped:
                return
            state, _ = self.ctrl.status()
            if state == "occupied":
                raise RuntimeError(OCCUPIED_MSG)
            if state == "ok":
                result = self.ctrl._request("POST", "/api/broadcast/stop", {}, timeout=120.0)
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "本地推流已停止，但 B站停播未完成，请确认直播间状态")
                self.ctrl.quit()
                if not self.ctrl.wait_closed():
                    raise RuntimeError("千音ai直播工具未正常退出，没有强制结束其他进程。")
            self.stopped = True

    def close(self):
        self.shutdown()
        self.window.destroy()
        return {"ok": True}

    def run(self):
        import webview
        self.ctrl.ensure_service()
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        self.window = webview.create_window(APP_TITLE, self.ctrl.base + "/", js_api=DesktopApi(self),
                                            width=1280, height=820, min_size=(1000, 700),
                                            x=60, y=30, background_color="#0b0d12")
        try:
            webview.start(gui="edgechromium", private_mode=True)
        finally:
            self.shutdown()


def main(argv=None):
    root = resolve_root(sys.argv[1:] if argv is None else argv)
    lock = SingleInstance(root)
    if not lock.acquire():
        focus_existing_window()
        return
    try:
        WebApp(root).run()
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, str(error), APP_TITLE, 0x10)


if __name__ == "__main__":
    main()
