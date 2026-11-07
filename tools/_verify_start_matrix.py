# 一次性组合矩阵 v4：IHateLivehime 配方走 bilibili_api 传输层，成功立即 stop
import asyncio
import hashlib
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bilibili_api.utils.network import Api
from bilibili_api.exceptions import ResponseCodeException
from bilibili_api import live as bili_live
from live_platform import BiliBroadcastPlatform, START_LIVE_URL

HIME_KEY = "aae92bc66f3edfab"
HIME_SEC = "af125a0d5279fd576c1b4418a3e8276d"
ANDROID_KEY = "1d8b6e7d45233436"
ANDROID_SEC = "560c52ccd288fed045859ed18bffd973"

cfg = json.load(open("config.json", encoding="utf-8"))
p = BiliBroadcastPlatform(cfg.get("bili_cookie", ""))
AREA = int((cfg.get("broadcast") or {}).get("area_id") or 283)


def app_sign(params: dict, sec: str) -> str:
    body = urllib.parse.urlencode(sorted(params.items()))
    return hashlib.md5((body + sec).encode()).hexdigest()


async def server_ts() -> int:
    try:
        data = await Api(url="https://api.bilibili.com/x/report/click/now",
                         method="GET").result
        return int((data or {}).get("now") or 0)
    except Exception:
        return 0


async def attempt(room_id, extra, key, sec, headers=None):
    data = {"appkey": key, "area_v2": AREA, "csrf": p.credential.bili_jct,
            "platform": "pc_link", "room_id": room_id, **extra}
    data["sign"] = app_sign(data, sec)
    api = Api(url=START_LIVE_URL, method="POST", verify=True, no_csrf=True,
              credential=p.credential)
    if headers:
        api = api.update_headers(**headers)
    return await api.update_data(**data).result


async def main():
    acc = await p.account()
    if int(acc.get("live_status") or 0) == 1:
        print("SKIP already_live")
        return
    room_id = int(acc["room_id"])
    ts = await server_ts()
    print("server_ts:", ts)
    HIME_VER = {"version": "7.47.0.10269", "build": "10269"}
    ANDROID_UA = ("Mozilla/5.0 BiliDroid/8.34.0 (bbcallen@gmail.com) os/android "
                  "model/FUCK mobi_app/android build/8340200 channel/oppo "
                  "innerVer/8340210 osVer/1145141919810 network/2")
    CANDS = [
        ("hime_pc_link+ts", {"ts": str(ts), **HIME_VER}, HIME_KEY, HIME_SEC, None, "pc_link"),
        ("hime_pc_link", dict(HIME_VER), HIME_KEY, HIME_SEC, None, "pc_link"),
        ("android64+ts", {"ts": str(ts)}, ANDROID_KEY, ANDROID_SEC,
         {"User-Agent": ANDROID_UA, "app-key": "android64"}, "android"),
    ]
    for name, extra, key, sec, headers, plat in CANDS:
        try:
            resp = await attempt(room_id, extra, key, sec, headers)
            if plat != "pc_link":
                resp = await attempt(room_id, {**extra, "platform": plat}, key, sec, headers)
        except ResponseCodeException as exc:
            raw = getattr(exc, "raw", None) or {}
            print("%-18s -> code=%s msg=%s data_keys=%s" % (
                name, exc.code, str(exc.msg or "")[:40],
                sorted((raw.get("data") or {}).keys())))
            qr = ((raw.get("data") or {}).get("qr") or "")
            if qr:
                print("    QR:", qr[:100])
            continue
        except Exception as exc:
            print("%-18s -> EXC %r" % (name, exc))
            continue
        rtmp = (resp.get("rtmp") or {})
        print("%-18s -> OK rtmp=%s" % (name, str(rtmp.get("addr") or "")[:40]))
        room = bili_live.LiveRoom(room_id, credential=p.credential)
        try:
            async with asyncio.timeout(15):
                await room.stop()
            print("STOPPED ok")
        except Exception as exc:
            print("STOP FAILED: %r" % exc)
        return
    print("all failed")


asyncio.run(main())
