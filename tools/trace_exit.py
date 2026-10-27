"""复现「走到某个节点后游戏自己退出」：带日志起游戏，逐个进入可达节点，盯住进程什么时候没了。

用法： python tools/trace_exit.py            # 让它跟着代打一起跑，只观察
"""
import json
import os
import subprocess
import sys
import time
import glob

GODOT = r"C:\rg\Godot_v4.7.2-stable_win64.exe"
PROJ = r"C:\gamedata\新建游戏项目"
LOG = r"C:\tmp\game_trace.log"
BRIDGE_HOST, BRIDGE_PORT = "127.0.0.1", 9787

import socket


def call(cmd, args=None, timeout=25):
    s = socket.create_connection((BRIDGE_HOST, BRIDGE_PORT), timeout=timeout)
    s.sendall((json.dumps({"cmd": cmd, "args": args or {}}, ensure_ascii=False) + "\n").encode("utf-8"))
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    r = json.loads(buf.decode("utf-8", "replace"))
    return r.get("screen", r)


def pids():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Godot_v4.7.2-stable_win64.exe"],
                         capture_output=True, text=True).stdout
    return [int(w) for w in out.split() if w.isdigit()]


def main():
    if "--attach" not in sys.argv:
        lf = open(LOG, "wb")
        subprocess.Popen([GODOT, "--path", PROJ], cwd=PROJ, stdout=lf, stderr=subprocess.STDOUT)
        lf.close()
        print("游戏已启动，日志 ->", LOG, flush=True)
    for _ in range(60):
        time.sleep(1)
        try:
            call("ping", timeout=2)
            break
        except Exception:
            continue
    else:
        print("桥没起来")
        return 1

    before = set(pids())
    last = None
    tail = 0
    while True:
        try:
            sc = call("screen", timeout=6).get("screen")
        except Exception as e:
            sc = "桥断开(%s)" % type(e).__name__
        alive = bool(before & set(pids()))
        if sc != last:
            last = sc
            print("%s screen=%s 游戏进程=%s" % (time.strftime("%H:%M:%S"), sc, "在" if alive else "没了"),
                  flush=True)
        if not alive:
            break
        time.sleep(1.0)

    raw = open(LOG, "rb").read() if os.path.exists(LOG) else b""
    print("\n=== 退出前日志（%d 字节）===" % len(raw))
    sys.stdout.buffer.write(raw[-2500:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
