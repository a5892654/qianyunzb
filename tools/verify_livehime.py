# -*- coding: utf-8 -*-
"""直播姬控制器离线验证：不启动直播姬、不发快捷键、不点击、不联网。"""
import sys
import tempfile
import traceback
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import live_livehime as livehime  # noqa: E402

_lines, _results = [], []


def emit(text=""):
    _lines.append(text)
    print(text, file=sys.stderr, flush=True)


def record(name, state, detail=""):
    _results.append((name, state, detail))
    emit("[%s] %s%s" % (state, name, (" :: " + detail) if detail else ""))


def req(condition, message):
    if not condition:
        raise AssertionError(message)


def test_parse():
    cases = {
        "F9": ((), 0x78, "F9"),
        "ctrl+alt+f9": ((0x11, 0x12), 0x78, "Ctrl+Alt+F9"),
        " Shift + Ctrl + s ": ((0x10, 0x11), 0x53, "Shift+Ctrl+S"),
        "PageUp": ((), 0x21, "PageUp"),
        "win+f24": ((0x5B,), 0x87, "Win+F24"),
        "0": ((), 0x30, "0"),
    }
    for spec, (mods, vk, label) in cases.items():
        hotkey = livehime.parse_hotkey(spec)
        req(hotkey.mods == mods and hotkey.vk == vk and hotkey.label == label,
            "parse %r -> %r" % (spec, hotkey))
    for bad in ("", "F25", "Ctrl+Alt", "Ctrl++", "不是键"):
        try:
            livehime.parse_hotkey(bad)
        except livehime.LivehimeError:
            continue
        raise AssertionError("应拒绝快捷键 %r" % bad)


def test_ratio():
    req(livehime._ratio_at(150, 60, (100, 20, 200, 100)) == (0.25, 0.4), "ratio 计算错误")
    req(livehime._ratio_at(99, 60, (100, 20, 200, 100)) is None, "窗口外应返回 None")
    req(livehime._ratio_at(1, 1, (0, 0, 0, 100)) is None, "零宽窗口应返回 None")


def test_path():
    root = Path(tempfile.mkdtemp(prefix="wt_livehime_path_"))
    exe = root / "livehime.exe"
    exe.write_bytes(b"")
    req(livehime.resolve_path(str(exe)) == (str(exe), "config"), "显式 exe 路径未识别")
    req(livehime.resolve_path(str(root))[0] == str(exe), "目录路径未找到 livehime.exe")
    req(livehime.resolve_path(str(root / "nope.exe")) == ("", "config_missing"), "缺失路径应明确报错")
    req(livehime._exe_in_dir(str(root)) == str(exe), "目录探测失败")


def test_check():
    root = Path(tempfile.mkdtemp(prefix="wt_livehime_check_"))
    exe = root / "livehime.exe"
    exe.write_bytes(b"")
    result = livehime.check({"path": str(exe), "hotkey": "F9"})
    req(result["ok"] and result["exists"] and result["hotkey_ok"], "check 正常路径: " + str(result))
    bad_key = livehime.check({"path": str(exe), "hotkey": "Ctrl+Alt"})
    req(not bad_key["hotkey_ok"] and bad_key["hotkey_error"], "check 应识别非法快捷键")
    missing = livehime.check({"path": "C:/nope/livehime.exe", "hotkey": "F9"})
    req(not missing["ok"] and "不存在" in missing["error"], "check 缺失路径: " + str(missing))


def test_choose_window():
    windows = [
        {"hwnd": 1, "title": "其他窗口", "visible": True, "minimized": False, "rect": [0, 0, 3000, 2000]},
        {"hwnd": 2, "title": "哔哩哔哩直播姬", "visible": True, "minimized": False, "rect": [0, 0, 1200, 800]},
        {"hwnd": 3, "title": "直播姬 - 弹幕", "visible": True, "minimized": False, "rect": [0, 0, 400, 300]},
    ]
    req(livehime.choose_window(windows)["hwnd"] == 2, "应优先选直播姬主窗口")
    req(livehime.choose_window([]) is None, "空列表应返回 None")


TESTS = [test_parse, test_ratio, test_path, test_check, test_choose_window]


def main():
    emit("== 直播姬控制器纯逻辑（不启动、不发送按键）")
    for test in TESTS:
        try:
            test()
            record(test.__name__, "PASS")
        except Exception as exc:
            record(test.__name__, "FAIL", "%s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc(limit=3)))
    failed = [name for name, state, _ in _results if state != "PASS"]
    emit()
    emit("合计 %d 项：通过 %d，失败 %d" % (len(_results), len(_results) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())