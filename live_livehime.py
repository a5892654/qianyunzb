"""直播姬（Livehime）控制：路径探测、窗口聚焦、开始/停止直播触发。

只做两件有副作用的事：启动已配置路径的 livehime.exe，以及向它发送用户
在直播姬「设置 → 快捷键」里绑定的开始/停止直播快捷键。不伪造 B 站接口。
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass

EXE_NAME = "livehime.exe"
COMMON_PATHS = (
    r"C:\Program Files\bililive\livehime\livehime.exe",
    r"C:\Program Files (x86)\bililive\livehime\livehime.exe",
    r"D:\Program Files\bililive\livehime\livehime.exe",
    r"D:\bililive\livehime\livehime.exe",
    r"E:\bililive\livehime\livehime.exe",
    r"F:\Ruanjian\bililive\livehime\livehime.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "livehime", "livehime.exe"),
)
TITLE_MARKS = ("直播姬", "livehime", "bilibili")

_MODIFIERS = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B, "windows": 0x5B,
}
_MOD_LABEL = {0x11: "Ctrl", 0x12: "Alt", 0x10: "Shift", 0x5B: "Win"}
_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA, "'": 0xDE,
    ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
}
_EXTENDED = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B}
_KEY_LABEL = {
    0x20: "Space", 0x0D: "Enter", 0x09: "Tab", 0x1B: "Esc", 0x08: "Backspace",
    0x2E: "Delete", 0x2D: "Insert", 0x24: "Home", 0x23: "End",
    0x21: "PageUp", 0x22: "PageDown", 0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0xBD: "-", 0xBB: "=", 0xDB: "[", 0xDD: "]", 0xDC: "\\", 0xBA: ";", 0xDE: "'",
    0xBC: ",", 0xBE: ".", 0xBF: "/", 0xC0: "`",
}
for _i in range(1, 25):
    _KEYS[f"f{_i}"] = 0x6F + _i
    _KEY_LABEL[0x6F + _i] = f"F{_i}"


class LivehimeError(Exception):
    pass


@dataclass(frozen=True)
class Hotkey:
    mods: tuple[int, ...]
    vk: int
    label: str


def parse_hotkey(spec: str) -> Hotkey:
    text = str(spec or "").strip()
    if not text:
        raise LivehimeError("请填写直播姬的开始/停止直播快捷键")
    parts = [p.strip().lower() for p in text.replace("＋", "+").split("+") if p.strip()]
    mods = []
    keys = []
    for part in parts:
        if part in _MODIFIERS:
            vk = _MODIFIERS[part]
            if vk not in mods:
                mods.append(vk)
        elif part in _KEYS:
            keys.append(_KEYS[part])
        elif len(part) == 1 and part.isalnum() and part.isascii():
            keys.append(ord(part.upper()))
        else:
            raise LivehimeError(f"不认识的快捷键：{part}（示例：F9 或 Ctrl+Alt+F9）")
    if len(keys) != 1:
        raise LivehimeError("快捷键需要且只能有一个主键（示例：F9 或 Ctrl+Alt+F9）")
    vk = keys[0]
    label = "+".join([_MOD_LABEL[m] for m in mods] + [_key_label(vk)])
    return Hotkey(tuple(mods), vk, label)


def _key_label(vk: int) -> str:
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    return _KEY_LABEL.get(vk, hex(vk))


if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
else:  # pragma: no cover - 本工具只发布 Windows 版
    _user32 = _kernel32 = None
    _ULONG_PTR = ctypes.c_ulong

_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SW_RESTORE = 9
_GA_ROOT = 2
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_EXTENDEDKEY = 0x0001
_INPUT_KEYBOARD = 1
_INPUT_MOUSE = 0
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", _ULONG_PTR),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class _TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


if _user32 is not None:
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.restype = wintypes.BOOL
    _user32.SetFocus.argtypes = [wintypes.HWND]
    _user32.SetFocus.restype = wintypes.HWND
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    _user32.SetCursorPos.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _user32.ClientToScreen.restype = wintypes.BOOL
    _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetClientRect.restype = wintypes.BOOL
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, _ULONG_PTR]
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL


def _window_text(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(max(2, length + 1))
    _user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def _process_elevated(pid: int) -> bool:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    token = wintypes.HANDLE()
    try:
        if not _advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
            return False
        info = _TOKEN_ELEVATION()
        size = wintypes.DWORD()
        if not _advapi32.GetTokenInformation(token, 20, ctypes.byref(info),
                                             ctypes.sizeof(info), ctypes.byref(size)):
            return False
        return bool(info.TokenIsElevated)
    finally:
        if token:
            _kernel32.CloseHandle(token)
        _kernel32.CloseHandle(handle)


def elevation_conflict() -> bool:
    """直播姬比千音权限高时，Windows 会丢弃模拟按键，快捷键永远发不进去。"""
    if _user32 is None or _process_elevated(os.getpid()):
        return False
    return any(_process_elevated(p["pid"]) for p in livehime_processes())


def _process_path(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def livehime_processes() -> list[dict]:
    if _kernel32 is None:
        return []
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        return []
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    out = []
    try:
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if str(entry.szExeFile).lower() == EXE_NAME:
                out.append({"pid": int(entry.th32ProcessID), "path": _process_path(int(entry.th32ProcessID))})
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return out


def windows_for_pids(pids) -> list[dict]:
    wanted = {int(pid) for pid in pids}
    out = []
    if _user32 is None or not wanted:
        return out
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) not in wanted:
            return True
        title = _window_text(hwnd)
        if not title:
            return True
        rect = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        out.append({"hwnd": int(hwnd), "pid": int(pid.value), "title": title,
                    "visible": bool(_user32.IsWindowVisible(hwnd)),
                    "minimized": bool(_user32.IsIconic(hwnd)),
                    "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]})
        return True

    _user32.EnumWindows(callback_type(collect), 0)
    return out


def choose_window(windows: list[dict]) -> dict | None:
    def score(w):
        title = str(w.get("title") or "").lower()
        area = max(0, w["rect"][2] - w["rect"][0]) * max(0, w["rect"][3] - w["rect"][1])
        return (sum(mark in title for mark in TITLE_MARKS) * 1000
                + (200 if w.get("visible") else 0)
                + (100 if not w.get("minimized") else 0)
                + min(area // 10000, 99))

    return max(windows, key=score) if windows else None


def _client_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wintypes.POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return int(origin.x), int(origin.y), int(rect.right - rect.left), int(rect.bottom - rect.top)


def _tap_alt() -> None:
    # 目标窗口刚被游戏抢过焦点时，Windows 会拒绝后台进程切换前台；
    # 模拟一次 Alt 键让系统把接下来的切换当作用户操作。
    _user32.keybd_event(0x12, 0, 0, 0)
    _user32.keybd_event(0x12, 0, _KEYEVENTF_KEYUP, 0)


def focus_window(hwnd: int) -> bool:
    if _user32 is None or not hwnd:
        return False
    _user32.ShowWindow(hwnd, _SW_RESTORE)
    current = _kernel32.GetCurrentThreadId()
    for _attempt in range(3):
        if int(_user32.GetForegroundWindow() or 0) == int(hwnd):
            return True
        foreground = _user32.GetForegroundWindow()
        fg_thread = int(_user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
        target_thread = int(_user32.GetWindowThreadProcessId(hwnd, None))
        attached = []
        for tid in (fg_thread, target_thread):
            if tid and tid != current and tid not in attached and _user32.AttachThreadInput(current, tid, True):
                attached.append(tid)
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetFocus(hwnd)
        finally:
            for tid in attached:
                _user32.AttachThreadInput(current, tid, False)
        time.sleep(0.15)
        root = _user32.GetAncestor(_user32.GetForegroundWindow(), _GA_ROOT)
        if int(root or 0) == int(hwnd):
            return True
        _tap_alt()
        time.sleep(0.1)
    return int(_user32.GetAncestor(_user32.GetForegroundWindow(), _GA_ROOT) or 0) == int(hwnd)


def _key_input(vk: int, keyup: bool) -> _INPUT:
    flags = (_KEYEVENTF_KEYUP if keyup else 0) | (_KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
    return _INPUT(type=_INPUT_KEYBOARD, ki=_KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0))


def send_hotkey(hotkey: Hotkey) -> None:
    if _user32 is None:
        raise LivehimeError("当前系统不支持发送快捷键")
    events = [_key_input(vk, False) for vk in hotkey.mods]
    events.append(_key_input(hotkey.vk, False))
    events.append(_key_input(hotkey.vk, True))
    events.extend(_key_input(vk, True) for vk in reversed(hotkey.mods))
    array = (_INPUT * len(events))(*events)
    sent = _user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))
    if sent != len(events):
        raise LivehimeError("快捷键发送失败（可能被系统权限拦截，请用管理员运行千音）")


def mouse_click(hwnd: int, x: int, y: int) -> None:
    if _user32 is None:
        raise LivehimeError("当前系统不支持鼠标点击")
    if not _user32.SetCursorPos(int(x), int(y)):
        raise LivehimeError("无法移动鼠标到直播姬窗口")
    down = _INPUT(type=_INPUT_MOUSE, mi=_MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_LEFTDOWN, 0, 0))
    up = _INPUT(type=_INPUT_MOUSE, mi=_MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_LEFTUP, 0, 0))
    events = (_INPUT * 2)(down, up)
    if _user32.SendInput(2, events, ctypes.sizeof(_INPUT)) != 2:
        raise LivehimeError("鼠标点击发送失败")


def _ratio_at(px: int, py: int, rect: tuple[int, int, int, int]) -> tuple[float, float] | None:
    left, top, width, height = rect
    if width <= 0 or height <= 0:
        return None
    x = (px - left) / width
    y = (py - top) / height
    return (x, y) if 0 <= x <= 1 and 0 <= y <= 1 else None


def _exe_in_dir(path: str) -> str:
    try:
        if not path or not os.path.isdir(path):
            return ""
        direct = os.path.join(path, EXE_NAME)
        if os.path.isfile(direct):
            return direct
        for entry in os.scandir(path):
            if entry.is_file() and entry.name.lower() == EXE_NAME:
                return entry.path
            if entry.is_dir():
                nested = os.path.join(entry.path, EXE_NAME)
                if os.path.isfile(nested):
                    return nested
    except OSError:
        pass
    return ""


def _registry_candidates():
    try:
        import winreg
    except ImportError:
        return
    roots = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for root, sub in roots:
        try:
            with winreg.OpenKey(root, sub) as key:
                count = winreg.QueryInfoKey(key)[0]
                for index in range(count):
                    try:
                        name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, name) as item:
                            display = str(winreg.QueryValueEx(item, "DisplayName")[0] or "")
                            location = str(winreg.QueryValueEx(item, "InstallLocation")[0] or "")
                            icon = str(winreg.QueryValueEx(item, "DisplayIcon")[0] or "")
                    except OSError:
                        continue
                    hint = (display + " " + location + " " + icon).lower()
                    if "直播姬" not in display and "livehime" not in hint:
                        continue
                    for candidate in (location, os.path.dirname(icon.strip('"').split(",")[0])):
                        found = _exe_in_dir(candidate)
                        if found:
                            yield found
        except OSError:
            continue


def resolve_path(explicit: str = "") -> tuple[str, str]:
    text = str(explicit or "").strip()
    if text:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(text)))
        if os.path.isdir(path):
            found = _exe_in_dir(path)
            return (found, "config") if found else ("", "config_missing")
        return (path, "config") if os.path.isfile(path) else ("", "config_missing")
    for proc in livehime_processes():
        if proc.get("path") and os.path.isfile(proc["path"]):
            return proc["path"], "running"
    for path in _registry_candidates():
        if os.path.isfile(path):
            return path, "registry"
    for path in COMMON_PATHS:
        path = os.path.expandvars(path)
        if os.path.isfile(path):
            return path, "common"
    return "", "not_found"


class LivehimeController:
    def __init__(self, app):
        self.app = app

    @property
    def settings(self) -> dict:
        return self.app.cfg["broadcast"].get("livehime") or {}

    def check(self, settings: dict | None = None) -> dict:
        values = settings if settings is not None else self.settings
        configured = str(values.get("path") or "")
        path, source = resolve_path(configured)
        procs = livehime_processes()
        windows = windows_for_pids([p["pid"] for p in procs]) if procs else []
        window = choose_window(windows)
        hotkey = str(values.get("hotkey") or "").strip()
        hotkey_ok, hotkey_error = True, ""
        if hotkey:
            try:
                parse_hotkey(hotkey)
            except LivehimeError as error:
                hotkey_ok = False
                hotkey_error = str(error)
        result = {"ok": bool(path or window), "path": path, "path_source": source,
                  "configured_path": configured, "exists": bool(path), "running": bool(procs),
                  "windows": windows, "window": window, "hotkey": hotkey,
                  "hotkey_ok": hotkey_ok, "hotkey_error": hotkey_error,
                  "self_elevated": _process_elevated(os.getpid()),
                  "elevated": any(_process_elevated(p["pid"]) for p in procs)}
        if source == "config_missing":
            # 已经跑着的窗口能控制，但配置的路径是错的，到点自动启动会失败，界面必须报出来。
            result["ok"] = False
            result["error"] = f"配置的直播姬路径不存在：{configured}"
        elif not procs and not path:
            result["error"] = "没有找到 livehime.exe，请填写直播姬安装目录里的 livehime.exe"
        return result

    def _ensure_running(self, timeout: float = 45.0) -> dict:
        deadline = time.monotonic() + max(3.0, float(timeout))
        procs = livehime_processes()
        window = choose_window(windows_for_pids([p["pid"] for p in procs])) if procs else None
        launched = False
        if not window and not procs:
            path, source = resolve_path(str(self.settings.get("path") or ""))
            if not path:
                reason = ("配置的直播姬路径不存在" if source == "config_missing"
                          else "找不到 livehime.exe")
                return {"ok": False, "error": reason + "：请填写直播姬安装目录里的 livehime.exe"}
            try:
                os.startfile(path)
            except OSError as error:
                return {"ok": False, "error": f"直播姬启动失败：{error}"}
            launched = True
        while time.monotonic() < deadline:
            procs = livehime_processes()
            window = choose_window(windows_for_pids([p["pid"] for p in procs])) if procs else None
            if window:
                focus_window(window["hwnd"])
                time.sleep(0.8)
                return {"ok": True, "launched": launched, "window": window}
            time.sleep(0.5)
        if procs:
            return {"ok": False, "error": "直播姬已在后台运行，但找不到主窗口；请从托盘打开后再试"}
        return {"ok": False, "error": "直播姬启动后没有出现窗口，请检查安装是否完整或是否需要登录"}

    async def ensure_running(self, timeout: float = 45.0) -> dict:
        import asyncio
        return await asyncio.to_thread(self._ensure_running, timeout)

    def trigger(self, action: str) -> dict:
        if elevation_conflict():
            return {"ok": False, "error": "直播姬以管理员权限运行，千音当前不是管理员，"
                                          "Windows 会拦截模拟的开播/停播按键；请用管理员身份运行千音，"
                                          "或取消直播姬的管理员权限后重启直播姬"}
        procs = livehime_processes()
        window = choose_window(windows_for_pids([p["pid"] for p in procs])) if procs else None
        if not window:
            return {"ok": False, "error": "直播姬没有在运行，无法发送开播/停播快捷键"}
        hwnd = window["hwnd"]
        if not focus_window(hwnd):
            return {"ok": False, "error": "无法把直播姬切到前台，已取消发送开播/停播键，避免按键落到游戏里；"
                                          "请解除窗口前台锁定后再试"}
        click = self._click_point()
        hotkey = str(self.settings.get("hotkey") or "").strip()
        if hotkey:
            try:
                send_hotkey(parse_hotkey(hotkey))
            except LivehimeError as error:
                if click:
                    mouse_click(hwnd, *click)
                    return {"ok": True, "method": "click", "window": window,
                            "message": "快捷键发送失败，已改为点击直播姬按钮"}
                return {"ok": False, "error": str(error)}
            return {"ok": True, "method": "hotkey", "hotkey": hotkey, "window": window,
                    "message": "已向直播姬发送" + ("开播" if action == "start" else "停播") + "快捷键"}
        if click:
            mouse_click(hwnd, *click)
            return {"ok": True, "method": "click", "window": window,
                    "message": "已点击直播姬的" + ("开始直播" if action == "start" else "停止直播") + "按钮"}
        return {"ok": False, "error": "请先在直播姬「设置 → 快捷键」给开始/停止直播绑定快捷键，并填到定时计划"}

    def _click_point(self) -> tuple[int, int] | None:
        try:
            x = float(self.settings.get("click_x") or 0)
            y = float(self.settings.get("click_y") or 0)
        except (TypeError, ValueError):
            return None
        if not (0.01 <= x <= 0.99 and 0.01 <= y <= 0.99):
            return None
        procs = livehime_processes()
        window = choose_window(windows_for_pids([p["pid"] for p in procs])) if procs else None
        if not window:
            return None
        left, top, width, height = _client_rect(window["hwnd"])
        return left + round(width * x), top + round(height * y)

    def cursor_ratio(self, delay: float = 3.0) -> dict:
        procs = livehime_processes()
        window = choose_window(windows_for_pids([p["pid"] for p in procs])) if procs else None
        if not window:
            return {"ok": False, "error": "直播姬没有在运行，无法校准按钮位置"}
        focus_window(window["hwnd"])
        time.sleep(max(0.5, float(delay)))
        point = wintypes.POINT()
        if not _user32.GetCursorPos(ctypes.byref(point)):
            return {"ok": False, "error": "读取鼠标位置失败"}
        rect = _client_rect(window["hwnd"])
        ratio = _ratio_at(int(point.x), int(point.y), rect)
        if ratio is None:
            return {"ok": False, "error": "鼠标不在直播姬窗口内，请把鼠标移到「开始直播」按钮上再等提示结束"}
        return {"ok": True, "x": round(ratio[0], 4), "y": round(ratio[1], 4), "window": window}


def check(settings: dict) -> dict:
    """不启动、不点击，仅探测路径/进程/窗口/快捷键格式，供界面诊断。"""
    controller = LivehimeController(type("_C", (), {"cfg": {"broadcast": {"livehime": settings or {}}}})())
    return controller.check()