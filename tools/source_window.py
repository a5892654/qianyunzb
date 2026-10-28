r"""「千音源画面」窗口：一块无边框 WebView2 画布，只显示 /source。

为什么不用 Edge --app：浏览器自己画的那条标题栏在客户区里面（实测把窗口的
WS_CAPTION 去掉它照样画），而窗口采集（WGC）采的是整个窗口，直播出去就带着那条
标题栏，画面还被它压掉三十来像素。WebView2 没有任何浏览器界面，客户区就是页面本身。

运行：.venv\Scripts\python.exe tools\source_window.py <url> <标题> <宽> <高> <x> <y>
"""
import sys


def main(argv):
    if len(argv) != 7:
        print("用法：source_window.py <url> <标题> <宽> <高> <x> <y>", file=sys.stderr)
        return 2
    url, title = argv[1], argv[2]
    w, h, x, y = (int(v) for v in argv[3:7])
    import webview
    webview.create_window(title, url, width=w, height=h, x=x, y=y,
                          frameless=True, resizable=False, on_top=False,
                          background_color="#000000")
    webview.start(gui="edgechromium", private_mode=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
