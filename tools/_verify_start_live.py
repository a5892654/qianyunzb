# 一次性开播自验：begin -> 校验推流地址 -> end（数秒内完成）
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live_platform import BiliBroadcastPlatform, PlatformError

cfg = json.load(open("config.json", encoding="utf-8"))
p = BiliBroadcastPlatform(cfg.get("bili_cookie", ""))


async def main():
    acc = await p.account()
    print("account:", {k: acc.get(k) for k in
                       ("logged_in", "name", "room_id", "live_status", "area_name", "title")})
    if not acc.get("logged_in"):
        print("RESULT: not_logged_in")
        return
    if int(acc.get("live_status") or 0) == 1:
        print("RESULT: already_live")
        return
    try:
        result = await p.begin()
    except PlatformError as exc:
        print("RESULT: begin_failed code=%s error=%s" % (getattr(exc, "code", 0), exc))
        return
    print("RESULT: begin_ok room_id=%s url_head=%s" % (result["room_id"], result["url"][:40]))
    await asyncio.sleep(2)
    stopped = await p.end()
    print("RESULT: end_ok stopped=%s" % stopped.get("stopped"))


asyncio.run(main())
