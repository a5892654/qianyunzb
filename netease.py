import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlencode

import aiohttp
from Cryptodome.Cipher import AES
from Cryptodome.PublicKey import RSA
from Cryptodome.Util.Padding import pad

log = logging.getLogger("song")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
BASE = "https://music.163.com"
# 网页登录使用 weapi + type=1，不能混用客户端 type=3 的登录链路。
# 协议与官方验证组件接入参考：https://github.com/Rainlxyl/netease-music-mcp-safe
QR_TYPE = 1
CAPTCHA_ID = "73a18dc827b24b18ad0783701a75277d"
DEVICE_APP_ID = "9d0ef7e0905d422cba1ecf7e73d77e67"
_WEAPI_KEY = b"0CoJUm6Qyw8W8jud"
_WEAPI_IV = b"0102030405060708"
_WEAPI_PUBLIC_KEY = RSA.import_key(b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/aKzmFbt7clFSs6sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQXE9/DDaFt6Rr7iVZMldczhC0JNgTz+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45wIDAQAB
-----END PUBLIC KEY-----""")


def _weapi_encrypt(payload):
    """网易网页协议：两层 AES-CBC 和 RSA 封装的单次随机密钥。"""
    secret = secrets.token_hex(8).encode("ascii")
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    first = base64.b64encode(AES.new(_WEAPI_KEY, AES.MODE_CBC, _WEAPI_IV).encrypt(pad(plain, 16)))
    params = AES.new(secret, AES.MODE_CBC, _WEAPI_IV).encrypt(pad(first, 16))
    wrapped = pow(int.from_bytes(secret[::-1], "big"), _WEAPI_PUBLIC_KEY.e, _WEAPI_PUBLIC_KEY.n)
    return {"params": base64.b64encode(params).decode("ascii"), "encSecKey": f"{wrapped:0256x}"}


class NetEase:
    def __init__(self, get_cookie):
        self.get_cookie = get_cookie
        self._qr = None

    def _headers(self, authenticated=True):
        h = {"User-Agent": UA, "Referer": BASE + "/"}
        cookie = (self.get_cookie() or "").strip() if authenticated else ""
        if cookie:
            if "=" not in cookie:
                cookie = f"MUSIC_U={cookie}"
            h["Cookie"] = cookie + "; os=pc"
        return h

    async def _get(self, path, params=None, raw=False, anonymous=False):
        try:
            async with aiohttp.ClientSession(headers=self._headers(authenticated=not anonymous)) as s:
                async with s.get(BASE + path, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                    if not isinstance(data, dict):
                        raise ValueError("网易云返回了无效数据，请稍后重试")
                    return (data, r) if raw else data
        except TimeoutError as e:
            raise RuntimeError("连接网易云超时，请检查网络后重试") from e
        except (aiohttp.ClientError, ValueError) as e:
            raise RuntimeError("连接网易云失败或返回异常，请稍后重试") from e

    async def search(self, kw, limit=15):
        d = await self._get("/api/search/get/web", {"s": kw, "type": 1, "limit": limit, "offset": 0})
        out = []
        for s in (d.get("result") or {}).get("songs") or []:
            out.append({"id": s["id"], "name": s["name"],
                        "artist": "、".join(a["name"] for a in s.get("artists") or []),
                        "album": (s.get("album") or {}).get("name", ""), "dur": round((s.get("duration") or 0) / 1000)})
        return out

    async def urls(self, ids, br=320000):
        d = await self._get("/api/song/enhance/player/url", {"ids": json.dumps(ids), "br": br})
        out = {}
        for it in d.get("data") or []:
            ok = bool(it.get("url")) and not it.get("freeTrialInfo")
            out[it["id"]] = it["url"] if ok else None
        return out

    async def url(self, song_id):
        return (await self.urls([song_id])).get(song_id)

    async def find(self, query, max_dur=0):
        """搜索并返回最匹配且能完整播放的歌；原词找不到就逐步放宽关键词找相似的。"""
        tried = []
        for kw in _variants(query):
            if kw in tried:
                continue
            tried.append(kw)
            try:
                songs = await self.search(kw)
                if max_dur:
                    songs = [s for s in songs if not s["dur"] or s["dur"] <= max_dur]
                if not songs:
                    continue
                playable = await self.urls([s["id"] for s in songs])
            except Exception as e:
                log.warning("搜索“%s”出错：%s", kw, e)
                continue
            ok = [s for s in songs if playable.get(s["id"])]
            if ok:
                best = max(ok, key=lambda s: _score(query, s))
                best["exact"] = _score(query, best) >= 2
                return best
        return None

    async def search_with_status(self, kw):
        songs = await self.search(kw, limit=20)
        if songs:
            playable = await self.urls([s["id"] for s in songs])
            for s in songs:
                s["playable"] = bool(playable.get(s["id"]))
        return songs

    async def download(self, url):
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
                r.raise_for_status()
                return await r.read()

    # ---------- 登录 ----------
    def cancel_qr(self):
        self._qr = None

    async def _qr_request(self, path, payload, context):
        headers = self._headers(authenticated=False)
        headers.update({"Origin": BASE, "x-os": "web", "X-loginMethod": "QrCode",
                        "x-login-chain-id": context["chain"],
                        "Cookie": "; ".join(f"{k}={v}" for k, v in context["cookies"].items())})
        url = BASE + "/weapi/" + path.removeprefix("/api/")
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(url, data=_weapi_encrypt({**payload, "csrf_token": ""}),
                                        timeout=aiohttp.ClientTimeout(total=15)) as response:
                    data = await response.json(content_type=None)
                    if not isinstance(data, dict):
                        raise ValueError("无效响应")
                    if response.status >= 400 and str(data.get("code")) not in ("800", "801", "802", "803", "8821", "8830"):
                        response.raise_for_status()
                    for item in (*response.history, response):
                        context["cookies"].update({k: v.value for k, v in item.cookies.items() if v.value})
                    body_cookie = data.get("cookie") or ""
                    for value in body_cookie if isinstance(body_cookie, list) else [body_cookie]:
                        parsed = SimpleCookie()
                        parsed.load(value)
                        context["cookies"].update({k: v.value for k, v in parsed.items() if v.value})
                    return data
        except TimeoutError as e:
            raise RuntimeError("连接网易云超时，请检查网络后重试") from e
        except (aiohttp.ClientError, ValueError, CookieError, TypeError) as e:
            raise RuntimeError("网易云登录接口连接失败或返回异常，请稍后重试") from e

    async def qr_key(self):
        stamp, nuid = time.time_ns() // 1_000_000, secrets.token_hex(16)
        context = {"chain": f"v1_unknown-{secrets.randbelow(1000000)}_web_login_{stamp}",
                   "cookies": {"_ntes_nuid": nuid, "_ntes_nnid": f"{nuid},{stamp}", "WEVNSM": "1.0.0"},
                   "key": None, "status": 801, "proofs": set(), "lock": asyncio.Lock()}
        self._qr = context
        d = await self._qr_request("/api/login/qrcode/unikey", {"type": QR_TYPE, "noCheckToken": True}, context)
        body = d.get("data") if isinstance(d.get("data"), dict) else d
        key = body.get("unikey")
        if str(d.get("code")) != "200" or not isinstance(key, str) or not key:
            raise RuntimeError("生成网易云二维码失败，请稍后重新扫码")
        if self._qr is not context:
            raise RuntimeError("二维码已更新，请使用最新二维码")
        context["key"] = key
        return key, BASE + "/st/platform/scanlogin?" + urlencode(
            {"codekey": key, "chainId": context["chain"], "hdw_device": "web", "hdw_appid": "web", "hitExp": "1"})

    async def qr_poll(self, key, secure_captcha="", yd_device_token=""):
        context = self._qr
        if not context or context["key"] != key:
            raise RuntimeError("二维码已更新或已结束，请重新扫码")
        for value in (secure_captcha, yd_device_token):
            if not isinstance(value, str) or len(value) > 8192 or any(c in value for c in "\r\n"):
                raise ValueError("安全验证参数无效，请重新完成验证")
        messages = {800: "二维码已过期，请重新扫码", 801: "等待扫码", 802: "已扫码，请在手机上确认", 803: "登录成功",
                    8821: "网易云要求安全验证，请点击“开始安全验证”后继续登录",
                    8830: "网易云要求进一步账号验证，请在官网完成登录后使用手动 Cookie 登录"}
        async with context["lock"]:
            if self._qr is not context:
                raise RuntimeError("二维码已更新或已结束，请重新扫码")
            if context["status"] == 8821 and not secure_captcha:
                return 8821, messages[8821], ""
            payload = {"key": key, "type": QR_TYPE, "noCheckToken": True, "ydDeviceToken": yd_device_token}
            if secure_captcha:
                digest = hashlib.sha256(secure_captcha.encode()).hexdigest()
                if context["status"] != 8821 or digest in context["proofs"]:
                    raise ValueError("安全验证已提交或当前无需验证，请重新打开安全验证")
                context["proofs"].add(digest)
                payload["secureCaptcha"] = secure_captcha
            d = await self._qr_request("/api/login/qrcode/client/login", payload, context)
            if self._qr is not context:
                raise RuntimeError("二维码已更新或已结束，请重新扫码")
            try:
                code = int(d.get("code"))
            except (TypeError, ValueError):
                code = None
            if code not in messages:
                raise RuntimeError(f"网易云扫码状态异常（{code or '未知'}），请重新扫码")
            context["status"] = code
            cookie = ""
            if code == 803:
                if not context["cookies"].get("MUSIC_U"):
                    raise RuntimeError("已确认扫码，但网易云未返回登录凭证，请重新扫码或手动粘贴 MUSIC_U")
                cookie = "; ".join(f"{k}={context['cookies'][k]}" for k in ("MUSIC_U", "__csrf") if context["cookies"].get(k))
            return code, messages[code], cookie

    async def account(self):
        d = await self._get("/api/nuser/account/get")
        prof, acc = d.get("profile") or {}, d.get("account") or {}
        if not prof:
            return None
        return {"nickname": prof.get("nickname", ""), "vip": int(acc.get("vipType") or prof.get("vipType") or 0)}


def _tokens(q):
    q = re.sub(r"[《》「」『』\"'“”]", " ", q.lower())
    return [p for p in re.split(r"\s*[-—–/|,，\s]+\s*", q) if p]


def _score(query, s):
    """2=歌名和歌手都对上（或没指定歌手），1=只有歌名对上，0=相似；翻唱/伴奏扣分。同分保留搜索排序。"""
    parts = _tokens(query)
    if not parts:
        return 2
    name, artist = s["name"].lower(), s["artist"].lower()
    if parts[0] not in name:
        return 0
    score = 2 if all(p in artist for p in parts[1:]) else 1
    if any(w in name for w in ("翻唱", "cover", "原唱", "伴奏", "dj", "remix")):
        score -= 0.5
    if re.sub(r"[（(\[【].*?[）)\]】]", "", name).strip() == parts[0]:
        score += 0.1
    return score


def _variants(q):
    q = q.strip()
    yield q
    plain = re.sub(r"[《》「」『』\"'“”]", " ", q).strip()
    yield plain
    parts = [p for p in re.split(r"\s*[-—–/|,，\s]+\s*", plain) if p]
    if len(parts) > 1:
        yield parts[0]
        yield " ".join(parts[:2])
    no_paren = re.sub(r"[（(\[【].*?[）)\]】]", "", plain).strip()
    if no_paren:
        yield no_paren
