r"""「千音源画面」窗口：默认使用共享内存 + 原生窗口；兼容独立网页模式。

兼容网页模式为什么不用 Edge --app：浏览器自己画的那条标题栏在客户区里面（实测把窗口的
WS_CAPTION 去掉它照样画），而窗口采集（WGC）采的是整个窗口，直播出去就带着那条
标题栏，画面还被它压掉三十来像素。WebView2 没有任何浏览器界面，客户区就是页面本身。

运行：.venv\Scripts\python.exe tools\source_window.py <url> <标题> <宽> <高> <x> <y> [--shared-memory <name>]
"""
import os
import sys


def main(argv):
    if len(argv) not in (7, 9):
        print("用法：source_window.py <url> <标题> <宽> <高> <x> <y>", file=sys.stderr)
        return 2
    url, title = argv[1], argv[2]
    w, h, x, y = (int(v) for v in argv[3:7])
    if len(argv) == 9:
        if argv[7] != "--shared-memory":
            return 2
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from tools.native_source import run
        return run(argv[8], title, w, h, x, y)
    # 此窗口经常被游戏挡住，但直播姬仍在采集它。只为本进程禁用浏览器的后台
    # 降速，避免观众看到降帧；保留调用方已有的 WebView2 参数。
    key = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    args = os.environ.get(key, "")
    for flag in ("--disable-background-timer-throttling",
                 "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"):
        if flag not in args.split():
            args = (args + " " + flag).strip()
    os.environ[key] = args
    import webview
    webview.create_window(title, url, width=w, height=h, x=x, y=y,
                          frameless=True, resizable=False, on_top=False,
                          background_color="#000000")
    webview.start(gui="edgechromium", private_mode=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
