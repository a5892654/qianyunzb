import asyncio
import http.cookies
import logging
import time

import aiohttp
import yarl

import blivedm
import blivedm.models.web as wm
from bilibili_api import Credential, user as bili_user

log = logging.getLogger("bili")
logging.getLogger("blivedm").setLevel(logging.ERROR)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
GUARD_NAMES = {1: "总督", 2: "提督", 3: "舰长"}
QR_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV = "https://api.bilibili.com/x/web-interface/nav"


def parse_cookie(s):
    s = (s or "").strip()
    if s and "=" not in s:
        return {"SESSDATA": s}
    out = {}
    for part in s.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k:
                # 浏览器“显示已解码”复制出来的 SESSDATA 带逗号，原样发送会被加引号导致无效
                out[k.strip()] = v.strip().strip('"').replace(",", "%2C")
    return out


def make_session(cookie_str):
    # 必须带 domain，否则 aiohttp 当成 bilibili.com 的 host-only cookie，api.bilibili.com 等子域名收不到
    jar = aiohttp.CookieJar()
    sc = http.cookies.SimpleCookie()
    for k, v in parse_cookie(cookie_str).items():
        sc[k] = v
        sc[k]["domain"] = "bilibili.com"
        sc[k]["path"] = "/"
    if sc:
        jar.update_cookies(sc)
    return aiohttp.ClientSession(cookie_jar=jar, headers={"User-Agent": UA})


class _Handler(blivedm.BaseHandler):
    _CMD_CALLBACK_DICT = blivedm.BaseHandler._CMD_CALLBACK_DICT.copy()

    def __init__(self, emit):
        self.emit = emit

    def _like(self, client, command):
        d = command.get("data") or {}
        self.emit({"type": "like", "uid": d.get("uid", 0), "name": d.get("uname", "")})

    _CMD_CALLBACK_DICT["LIKE_INFO_V3_CLICK"] = _like

    def _wishlist(self, client, command):
        # WIDGET_WISH_LIST / WIDGET_WISH_INFO 的顶层 wish_status=2 才是整单达成。
        # wish_status_info 是状态说明表，其中始终包含“达成”，不能用它判断完成。
        # 消息样例：https://janson20.github.io/bilibili-api-collect-mirror/docs/live/message_stream.html
        d = command.get("data")
        if not isinstance(d, dict):
            return
        try:
            sid, status = int(d.get("sid", 0)), int(d.get("wish_status", -1))
        except (TypeError, ValueError):
            return
        if sid <= 0 or status not in (-1, 0, 1, 2, 3):
            return
        items = []
        wishes = d.get("wish") or []
        for item in wishes if isinstance(wishes, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                target = max(0, int(item.get("target_num") or 0))
                current = max(0, int(item.get("current_num") or 0))
                done = int(item.get("wish_status") or 0) == 2
            except (TypeError, ValueError):
                continue
            items.append({"name": str(item.get("gift_name") or "礼物")[:80],
                          "current": current, "target": target, "completed": done})
        self.emit({"type": "wishlist", "room_id": client.room_id, "wish_id": str(sid),
                   "wish_name": str(d.get("wish_name") or "直播心愿单")[:80],
                   "status": status, "items": items, "wish_ts": d.get("ts")})

    _CMD_CALLBACK_DICT["WIDGET_WISH_LIST"] = _wishlist
    _CMD_CALLBACK_DICT["WIDGET_WISH_INFO"] = _wishlist

    def _on_danmaku(self, client, m: wm.DanmakuMessage):
        self.emit({"type": "danmaku", "uid": m.uid, "name": m.uname, "text": m.msg,
                   "emoticon": m.dm_type == 1, "guard_level": m.privilege_type})

    def _on_gift(self, client, m: wm.GiftMessage):
        paid = m.coin_type == "gold"
        self.emit({"type": "gift", "uid": m.uid, "name": m.uname, "gift": m.gift_name, "num": m.num,
                   "value": round(m.total_coin / 1000, 2) if paid else 0.0, "paid": paid,
                   "room_id": getattr(client, "room_id", None), "gift_id": m.gift_id,
                   "gift_tid": m.tid, "gift_rnd": m.rnd})

    def _on_user_toast_v2(self, client, m: wm.UserToastV2Message):
        if m.source == 2:
            return
        self.emit({"type": "guard", "uid": m.uid, "name": m.username, "level": m.guard_level,
                   "guard": GUARD_NAMES.get(m.guard_level, "舰长"), "num": m.num or 1,
                   "value": round(m.price / 1000, 2)})

    def _on_buy_guard(self, client, m: wm.GuardBuyMessage):
        self.emit({"type": "guard", "uid": m.uid, "name": m.username, "level": m.guard_level,
                   "guard": GUARD_NAMES.get(m.guard_level, "舰长"), "num": m.num or 1,
                   "value": round(m.price / 1000, 2)})

    def _on_super_chat(self, client, m: wm.SuperChatMessage):
        self.emit({"type": "superchat", "uid": m.uid, "name": m.uname, "text": m.message,
                   "price": m.price, "value": float(m.price)})

    def _on_interact_word_v2(self, client, m: wm.InteractWordV2Message):
        kind = {1: "enter", 2: "follow", 4: "follow", 5: "follow"}.get(m.msg_type)
        if kind:
            self.emit({"type": kind, "uid": m.uid, "name": m.username,
                       "room_id": getattr(client, "room_id", None)})


class BiliLive:
    def __init__(self, on_event, on_status):
        self.on_event = on_event
        self.on_status = on_status
        self.session = None
        self.client = None
        self.room_id = None
        self.owner_uid = None
        self.login_uid = None
        self.login_name = ""
        self.login_error = ""
        self.connected = False
        self._watch = None
        self._follow_cache = {}
        self._cookie = ""

    def _emit(self, ev):
        ev["ts"] = time.time()
        if ev.get("type") == "follow" and ev.get("uid") and ev.get("room_id"):
            self._follow_cache[(str(ev["room_id"]), str(ev["uid"]))] = time.monotonic() + 60
        if ev.get("name"):
            ev["name"] = str(ev["name"]).strip()
        try:
            self.on_event(ev)
        except Exception:
            log.exception("event handler failed")

    async def start(self, room_id, cookie):
        await self.stop()
        self._cookie = cookie
        self.session = make_session(cookie)
        if parse_cookie(cookie):
            await self.refresh_login()
            if self.login_name:
                self.on_status(f"B站登录成功：{self.login_name}（UID {self.login_uid}）")
            else:
                self.on_status(f"B站登录无效：{self.login_error or 'Cookie 已过期或不完整'}，请重新扫码登录")
        self.client = blivedm.BLiveClient(int(room_id), session=self.session)
        self.client.set_handler(_Handler(self._emit))
        self.client.start()
        self._watch = asyncio.create_task(self._watch_init(room_id))

    async def _watch_init(self, room_id):
        for _ in range(100):
            await asyncio.sleep(0.2)
            if self.client and self.client.room_owner_uid:
                break
        if not self.client:
            return
        self.room_id = self.client.room_id
        self.owner_uid = self.client.room_owner_uid
        self.connected = bool(self.owner_uid)
        if self.connected:
            who = f"已登录：{self.login_name}" if self.login_name else "未登录B站（观众名字会被打码）"
            self.on_status(f"已连接直播间 {self.room_id}，{who}")
        else:
            self.on_status(f"连接直播间 {room_id} 失败，请检查房间号或网络")

    async def stop(self):
        if self._watch:
            self._watch.cancel()
            self._watch = None
        if self.client:
            try:
                await self.client.stop_and_close()
            except Exception:
                pass
            self.client = None
        if self.session:
            await self.session.close()
            self.session = None
        self.connected = False
        self._follow_cache.clear()
        self._cookie = ""

    async def is_following(self, uid):
        """先用当前房间的新关注消息，否则查询观众是否关注已登录的主播。"""
        if not self.connected or not self.owner_uid:
            raise RuntimeError("请先连接主播的直播间")
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            uid = 0
        if uid <= 0:
            raise RuntimeError("无法识别观众 UID，请登录主播账号后重新连接直播间")
        context = (self.session, self.room_id, self.owner_uid, self.login_uid)
        key = (str(self.room_id), str(uid))
        if self._follow_cache.get(key, 0) > time.monotonic():
            return True
        if str(self.login_uid or "") != str(self.owner_uid):
            raise RuntimeError("请登录当前直播间的主播账号，以核实老观众的关注状态")
        cookies = parse_cookie(self._cookie)
        credential = Credential(sessdata=cookies.get("SESSDATA"), bili_jct=cookies.get("bili_jct"),
                                buvid3=cookies.get("buvid3"), buvid4=cookies.get("buvid4"),
                                dedeuserid=cookies.get("DedeUserID"), ac_time_value=cookies.get("ac_time_value"))
        try:
            async with asyncio.timeout(8):
                relation = await bili_user.User(uid, credential=credential).get_relation()
            # be_relation 是观众 -> 当前登录主播；relation 则是主播 -> 观众。
            attribute = int(relation["be_relation"]["attribute"])
        except Exception:
            raise RuntimeError("关注状态查询失败，请检查 B站登录状态或稍后重试") from None
        if not self.connected or context != (self.session, self.room_id, self.owner_uid, self.login_uid):
            raise RuntimeError("直播间连接已改变，请重新点歌")
        # 查询过程中收到的新关注消息比接口的短暂延迟更可靠。
        following = attribute in (1, 2, 6) or self._follow_cache.get(key, 0) > time.monotonic()
        if following:
            self._follow_cache[key] = time.monotonic() + 60
        return following

    async def refresh_login(self):
        self.login_uid, self.login_name, self.login_error = None, "", ""
        try:
            async with self.session.get(NAV, timeout=aiohttp.ClientTimeout(total=10)) as r:
                j = await r.json(content_type=None)
            d = j.get("data") or {}
            if d.get("isLogin"):
                self.login_uid, self.login_name = d.get("mid"), d.get("uname", "")
            else:
                self.login_error = f"B站返回 {j.get('code')} {j.get('message', '')}".strip()
        except Exception as e:
            self.login_error = f"检查登录失败：{e}"
            log.warning("nav check failed: %s", e)


SMS_URLS = {
    "captcha": "https://passport.bilibili.com/x/passport-login/captcha",
    "send": "https://passport.bilibili.com/x/passport-login/web/sms/send",
    "login": "https://passport.bilibili.com/x/passport-login/web/login/sms",
    "buvid": "https://api.bilibili.com/x/frontend/finger/spi",
}
COOKIE_KEYS = ("DedeUserID", "DedeUserID__ckMd5", "SESSDATA", "bili_jct", "sid", "refresh_token")
# B站短信相关错误码，只留用户看得懂的那几句
SMS_ERRORS = {
    5: "短信验证码不对，重新输入或再发一次",
    -5: "短信验证码不对或已过期",
    86: "发送太频繁，请稍后再试",
    89: "请求过于频繁，稍后再试",
    100003: "这个手机号没有注册过B站",
    215971: "人机验证已过期，请重新点「获取验证码」",
    -412: "被B站风控拦住了，请改用扫码登录",
}


def sms_error(j: dict, default: str = "B站返回错误") -> str:
    return SMS_ERRORS.get(j.get("code"), str(j.get("message") or default))


def send_params(phone: str, gee: dict, token: str, challenge: str) -> dict:
    """字段名按B站网页端：极验的 validate/seccode 加上它给的 token/challenge。"""
    return {"source": "main-fe-header", "cid": "86", "tel": phone, "token": token,
            "challenge": challenge, "validate": str(gee.get("validate") or ""),
            "seccode": str(gee.get("seccode") or "")}


def login_params(phone: str, code: str, captcha_key: str) -> dict:
    return {"source": "main_web", "cid": "86", "tel": phone, "code": code,
            "captcha_key": captcha_key, "keep": "true"}


def parse_captcha(j: dict) -> dict:
    """极验参数；B站也可能改发腾讯验证码，那就只能走扫码。"""
    if j.get("code") != 0:
        raise RuntimeError(sms_error(j, "获取人机验证失败"))
    data = j.get("data") or {}
    gee = data.get("geetest") or {}
    if not gee.get("gt"):
        raise RuntimeError("B站这次给的是别的人机验证，请改用扫码登录")
    return {"gt": gee["gt"], "challenge": gee.get("challenge", ""), "token": data.get("token", "")}


def parse_send(j: dict) -> str:
    if j.get("code") != 0:
        raise RuntimeError(sms_error(j, "验证码发送失败"))
    key = (j.get("data") or {}).get("captcha_key") or ""
    if not key:
        raise RuntimeError("B站没回 captcha_key，请重新滑一次人机验证")
    return key


def parse_login(j: dict, cookies: dict) -> str:
    if j.get("code") != 0:
        raise RuntimeError(sms_error(j, "登录失败"))
    if not cookies.get("SESSDATA"):
        # status=1 是B站要求二次安全验证，那条流程不在这里做
        raise RuntimeError("B站要求额外安全验证，没给登录态，请改用扫码登录")
    return "; ".join(f"{k}={cookies[k]}" for k in COOKIE_KEYS if k in cookies)


class SmsLogin:
    """B站手机号 + 短信验证码登录：极验 -> 发验证码 -> 换 Cookie。

    极验必须真人滑完才拿得到 validate，所以那一步在前端做；手机号只在单次请求里
    用到，不写进配置、不写日志。
    """

    @property
    def _headers(self):
        return {"User-Agent": UA, "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com"}

    async def captcha(self) -> dict:
        async with aiohttp.ClientSession(headers=self._headers) as s:
            async with s.get(SMS_URLS["captcha"],
                             params={"source": "main-fe-header", "t": int(time.time() * 1000)},
                             timeout=aiohttp.ClientTimeout(total=12)) as r:
                return parse_captcha(await r.json(content_type=None))

    async def send(self, phone: str, gee: dict, token: str, challenge: str) -> str:
        async with aiohttp.ClientSession(headers=self._headers) as s:
            async with s.get(SMS_URLS["buvid"], timeout=aiohttp.ClientTimeout(total=12)) as r:
                buvid = ((await r.json(content_type=None)).get("data") or {}).get("b_3") or ""
            async with s.post(SMS_URLS["send"], data=send_params(phone, gee, token, challenge),
                              cookies={"buvid3": buvid} if buvid else {},
                              timeout=aiohttp.ClientTimeout(total=12)) as r:
                return parse_send(await r.json(content_type=None))

    async def login(self, phone: str, code: str, captcha_key: str) -> str:
        async with aiohttp.ClientSession(headers=self._headers) as s:
            async with s.post(SMS_URLS["login"], data=login_params(phone, code, captcha_key),
                              timeout=aiohttp.ClientTimeout(total=12)) as r:
                j = await r.json(content_type=None)
                return parse_login(j, {k: v.value for k, v in r.cookies.items() if k in COOKIE_KEYS})


class QrLogin:
    """B站网页扫码登录：生成二维码 -> 轮询 -> 拿到 Cookie。"""

    async def generate(self):
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as s:
            async with s.get(QR_GEN, timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = (await r.json(content_type=None))["data"]
        return d["url"], d["qrcode_key"]

    async def poll(self, key):
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as s:
            async with s.get(QR_POLL, params={"qrcode_key": key}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = (await r.json(content_type=None))["data"]
                code = d.get("code")
                if code != 0:
                    msg = {86101: "等待扫码", 86090: "已扫码，请在手机上确认", 86038: "二维码已过期"}.get(code, d.get("message", ""))
                    return code, msg, ""
                cookies = {k: v.value for k, v in r.cookies.items()}
        if not cookies.get("SESSDATA"):
            raw = yarl.URL(d.get("url", "")).raw_query_string
            q = dict(p.split("=", 1) for p in raw.split("&") if "=" in p)
            cookies = {k: q[k] for k in ("DedeUserID", "DedeUserID__ckMd5", "SESSDATA", "bili_jct") if k in q}
        return 0, "登录成功", "; ".join(f"{k}={v}" for k, v in cookies.items())
