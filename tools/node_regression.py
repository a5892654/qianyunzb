import json
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 9787
OVERLAYS = {"fountain", "arena", "rift", "shrine", "chest", "soul_forge", "dungeon",
            "dialog", "event", "campfire", "shop", "loot", "reward",
            "death", "death_analysis", "run_end"}
STUCK_S = 25


def call(cmd, timeout=8):
    with socket.create_connection((HOST, PORT), timeout=timeout) as sock:
        sock.sendall((json.dumps({"cmd": cmd, "args": {}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    response = json.loads(buf.decode("utf-8"))
    return response[cmd]


def readable(body):
    return isinstance(body, dict) and any(body.get(key) for key in
        ("options", "buttons", "texts", "title", "desc", "cards", "choices", "items", "offers", "tiles"))


def main():
    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
    seen, blocks, unreadable = {}, set(), set()
    started = time.monotonic()
    last, revision, since = None, None, started
    reported_stuck = False
    samples = 0
    print("只读观察 %ds，不发送点击或关闭命令" % budget, flush=True)
    while time.monotonic() - started < budget:
        try:
            state = call("state")
        except (OSError, ValueError, KeyError) as error:
            print("桥读取失败:", type(error).__name__, error, flush=True)
            time.sleep(0.5)
            continue
        samples += 1
        now = time.monotonic()
        name = state.get("screen")
        body = state.get(name) or {}
        identity = (name, state.get("window_id", ""))
        current_revision = body.get("revision") if isinstance(body, dict) else None
        if state.get("blocked"):
            blocks.add((name, state.get("top")))
        if identity != last:
            if last and last[0] in OVERLAYS:
                seen[last[0]]["departed"] += 1
            last, revision, since = identity, current_revision, now
            reported_stuck = False
            rec = seen.setdefault(name, {"n": 0, "departed": 0, "stalled": 0})
            rec["n"] += 1
            print("进入 %s window=%s top=%s" % (name, identity[1], state.get("top")), flush=True)
        elif current_revision != revision:
            revision, since = current_revision, now
            reported_stuck = False
        if name in OVERLAYS:
            if not readable(body):
                if identity not in unreadable:
                    print("FAIL: %s 窗口没有可读正文或操作" % name, flush=True)
                unreadable.add(identity)
            if not reported_stuck and now - since > STUCK_S:
                seen[name]["stalled"] += 1
                reported_stuck = True
                print("FAIL: %s 同一窗口状态超过 %ds 未推进；不代替模型救援" % (name, STUCK_S), flush=True)
        time.sleep(0.5)

    print(json.dumps({"samples": samples, "screens": seen,
                      "blocked": sorted(blocks, key=str), "unreadable": sorted(unreadable, key=str)},
                     ensure_ascii=False, indent=2))
    covered = [name for name in seen if name in OVERLAYS]
    if not samples or not covered:
        print("INCONCLUSIVE: 未读到窗口，不能宣称通过")
        return 2
    if blocks or unreadable or any(seen[name]["stalled"] for name in covered):
        print("FAIL: 存在遮挡误判、空状态或窗口未推进")
        return 1
    print("PASS: 本次见到的窗口均可读，未观察到超时卡住；未验证未出现的窗口")
    return 0


if __name__ == "__main__":
    sys.exit(main())
