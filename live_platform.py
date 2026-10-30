"""B站本人直播间的正常授权开播接口。"""

import asyncio
import hashlib
import ipaddress
import re
import time
import urllib.parse

from bili import parse_cookie
from bilibili_api import live as bili_live
from bilibili_api import user as bili_user
from bilibili_api.exceptions import NetworkException, ResponseCodeException
from bilibili_api.utils.network import Api, Credential
from bilibili_api.utils.picture import Picture

LiveRoom = bili_live.LiveRoom

ROOM_UPDATE_URL = "https://api.live.bilibili.com/room/v1/Room/update"
START_LIVE_URL = "https://api.live.bilibili.com/room/v1/Room/startLive"
LIVE_TS_URL = "https://api.bilibili.com/x/report/click/now"
# 直播姬通道的开播形态（社区 IHateLivehime 实测可用）：缺 version/build/ts
# 会被 B站按 60024 拒绝；用静态旧版本走二维码认证，最新版会转 60043 外部验证
START_CLIENT = {"version": "7.47.0.10269", "build": "10269"}
START_APPKEY = "aae92bc66f3edfab"
START_APPSEC = "af125a0d5279fd576c1b4418a3e8276d"
PRE_LIVE_INFO_URL = "https://api.live.bilibili.com/xlive/app-blink/v1/preLive/UpdatePreLiveInfo"
TIMEOUT_SECONDS = 15.0
PICTURE_TIMEOUT_SECONDS = 30.0

# B站错误码 -> 中文人工处理提示（只保留安全文案，不携带原始响应）
MANUAL_HINTS = {
    -352: "触发 B站风控（-352），请在浏览器登录 B站网页端人工处理后再试",
    -412: "触发 B站风控（-412），请在浏览器完成人工验证后再试",
    -101: "B站登录状态无效或未登录，请重新扫码登录",
    60013: "账号未完成实名认证，请先在 B站完成实名认证后再开播",
    60024: "开播需要人脸认证，请按界面提示用B站App扫码完成验证",
    60034: "该分区目前仅支持直播姬开播，请手动在直播姬开始直播",
    60037: "B站限制该账号通过网页开播，请在直播姬或网页端人工处理",
    65530: "B站登录已过期，请重新扫码登录后再试",
}
NO_ROOM_HINT = "该账号还没有个人直播间，请先在 B站直播中心开通直播间"
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SECRETISH_RE = re.compile(r"rtmp|flv|push|stream_key|live_key|https?://|ws://", re.I)


class PlatformError(Exception):
    """只含人类可读安全信息的异常；str() 绝不含 rtmp 地址/key/原始 data/Cookie。"""

    def __init__(self, message: str, code=None):
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self):
        return self.message


class FaceAuthRequiredError(PlatformError):
    """60024：本次开播需要人脸认证。qr 是B站官方返回的认证二维码地址，
    扫码完成的是B站自己的实名验证流程，验证通过后该通道才能开播。"""

    def __init__(self, message: str, qr: str):
        super().__init__(message, 60024)
        self.qr = qr


def _bili_msg(exc) -> str:
    """B站自己回的那句话。错误码含义要靠它核对（同一个码在不同接口意思可能不一样）。
    只取 msg 且必须是字符串，去控制符后截断——raw 里可能带推流地址，绝不能进文案。"""
    raw = getattr(exc, "msg", None)
    if not isinstance(raw, str):
        return ""
    text = _CTRL_RE.sub("", raw).strip()[:60]
    # B站偶尔把推流地址/密钥塞在 message 里，宁可只报错误码，也不能把它显示出去
    return "" if _SECRETISH_RE.search(text) else text


def _friendly(exc: Exception) -> PlatformError:
    """把底层异常转换为精简 PlatformError：人工提示 + 错误码 + B站原话。"""
    if isinstance(exc, PlatformError):
        return exc
    if isinstance(exc, asyncio.TimeoutError):
        return PlatformError("请求 B站超时，请检查网络后稍后重试")
    if isinstance(exc, ResponseCodeException):
        try:
            code = int(exc.code)
        except (TypeError, ValueError):
            code = 0
        detail = _bili_msg(exc)
        hint = MANUAL_HINTS.get(code) or "B站接口请求失败（code=%s），请稍后重试" % code
        return PlatformError(hint + ("（B站：%s）" % detail if detail else ""), code)
    if isinstance(exc, NetworkException):
        if getattr(exc, "status", 0) == 412:
            return PlatformError(MANUAL_HINTS[-412], 412)
        return PlatformError("连接 B站失败，请检查网络后重试")
    return PlatformError("B站请求失败，请稍后重试")


## B站没有对外的「直播封面上传」接口（x/upload/web/image 一律 -400），
## 网页直播中心实际走的是动态图床，所以复用库里的 Picture.upload。
## 它返回的是 http 地址，而直播间只认 https，这里统一升级协议。
async def _upload_cover_picture(data: bytes, mime: str, cred: Credential) -> str:
    fmt = {"image/png": "png", "image/webp": "webp"}.get((mime or "").lower(), "jpg")
    picture = Picture.from_content(data, fmt)
    try:
        async with asyncio.timeout(PICTURE_TIMEOUT_SECONDS):
            await picture.upload(cred)
    except asyncio.TimeoutError:
        raise PlatformError("封面图片上传超时，请稍后重试") from None
    except Exception as exc:
        raise PlatformError("封面上传失败：%s" % _friendly(exc)) from None
    url = str(picture.url or "")
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if not url.startswith("https://"):
        raise PlatformError("B站图床没有返回图片地址，请稍后重试")
    return url


def _validate_push_url(addr: str, code: str) -> str:
    """仅接受官方接口返回的网络推流目标。"""
    if _CTRL_RE.search(addr) or _CTRL_RE.search(code):
        raise PlatformError("开播接口返回的推流地址包含非法字符，请人工核查后重试")
    if not addr or not code:
        raise PlatformError("开播接口未返回有效推流地址（rtmp 缺失）")
    try:
        parsed = urllib.parse.urlparse(addr)
    except ValueError:
        raise PlatformError("开播接口返回的推流地址格式异常，请人工核查后重试")
    if parsed.scheme not in ("rtmp", "rtmps") or not parsed.netloc:
        raise PlatformError("开播接口返回的推流地址协议或主机无效，请人工核查后重试")
    host = (parsed.hostname or "").lower()
    if "@" in parsed.netloc:
        raise PlatformError("开播接口返回的推流地址内嵌账号信息，已拒绝使用")
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise PlatformError("开播接口返回的推流地址指向本机，已拒绝使用")
    try:
        ipaddress.ip_address(host)  # 解析成功说明是 IP 字面量
    except ValueError:
        pass
    else:
        # 不接受任何 IP 字面量（含回环/私网段，无法逐一判断则整体拒绝更安全）
        raise PlatformError("开播接口返回的推流地址为 IP 字面量，已拒绝使用")
    base = addr if addr.endswith("/") else addr + "/"
    return base + code.lstrip("/")


def _app_sign(params: dict) -> str:
    body = urllib.parse.urlencode(sorted(params.items()))
    return hashlib.md5((body + START_APPSEC).encode()).hexdigest()


def _notice_text(info: dict) -> str:
    news = info.get("news")
    if isinstance(news, dict):
        return str(news.get("content") or "")
    return str(news or "")


class BiliBroadcastPlatform:
    """以扫码登录的主播 Cookie 操作其本人直播间的开播/关播。"""

    def __init__(self, cookie: str):
        self.cookie = cookie or ""
        d = parse_cookie(self.cookie)
        self.credential = Credential(
            sessdata=d.get("SESSDATA"),
            bili_jct=d.get("bili_jct"),
            buvid3=d.get("buvid3"),
            buvid4=d.get("buvid4"),
            dedeuserid=d.get("DedeUserID"),
            ac_time_value=d.get("ac_time_value"),
        )
        self.room = None  # begin 时确认的本人房间 LiveRoom
        self.started_room_id = 0  # 仅记录本实例成功开播的房间

    async def account(self) -> dict:
        """读取当前登录主播本人信息；未登录返回 logged_in=False，不读他人 Cookie。"""
        if self.credential.sessdata is None:
            return {"ok": True, "logged_in": False, "name": "",
                    "uid": 0, "room_id": 0, "live_status": 0}
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                me = await bili_user.get_self_info(self.credential)
                uid = int((me or {}).get("mid") or 0)
                if uid <= 0:
                    raise PlatformError("登录身份读取失败，请重新扫码登录")
                live_info = await bili_user.User(
                    uid, credential=self.credential).get_live_info()
        except Exception as exc:
            raise _friendly(exc) from None
        room = (live_info or {}).get("live_room") or {}
        result = {
            "ok": True,
            "logged_in": True,
            "name": str((me or {}).get("name") or (me or {}).get("uname") or ""),
            "uid": uid,
            "room_id": int(room.get("roomid") or 0),
            "live_status": int(room.get("liveStatus") or 0),
        }
        if result["room_id"]:
            try:
                async with asyncio.timeout(TIMEOUT_SECONDS):
                    details = await LiveRoom(result["room_id"], credential=self.credential).get_room_info()
                info = details["room_info"]
                if int(info["uid"]) != uid:
                    raise PlatformError("直播间不属于当前登录账号，已拒绝开播")
                result["room_id"] = int(info["room_id"])
                result["live_status"] = int(info["live_status"])
                result["title"] = str(info.get("title") or "")
                result["cover"] = str(info.get("cover") or "")
                # room_info.cover 有时指向已清理的动态链（会 404），keyframe 通常还在
                result["cover_fallback"] = str(info.get("keyframe") or info.get("user_cover") or "")
                result["notice"] = _notice_text(info)
                result["area_name"] = str(info.get("area_name") or "")
            except Exception as error:
                raise _friendly(error) from None
        if not result["room_id"]:
            result["error"] = NO_ROOM_HINT
        return result

    async def areas(self) -> list:
        """公开读取直播分区，无凭证、无写操作。"""
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                data = await bili_live.get_area_info()
        except Exception as exc:
            raise _friendly(exc) from None
        out = []
        for parent in data:
            for area in parent["list"]:
                if str(area.get("lock_status", "0")) == "0":
                    out.append({"id": int(area["id"]), "name": str(area["name"]), "parent": str(parent["name"])})
        return out

    async def _update_title(self, room_id: int, title: str) -> None:
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                await Api(url=ROOM_UPDATE_URL, method="POST", verify=True,
                          credential=self.credential).update_data(
                    room_id=room_id, title=title).result
        except Exception as exc:
            raise _friendly(exc) from None

    async def _stop_started_room(self) -> bool:
        """关闭本实例刚开启的房间；成功 True，失败 False 且保留 started_room_id 供重试。"""
        room_id = self.started_room_id
        if not room_id:
            return True
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                await LiveRoom(room_id, credential=self.credential).stop()
        except Exception:
            return False
        self.started_room_id = 0
        return True

    async def _server_ts(self) -> str:
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                data = await Api(url=LIVE_TS_URL, method="GET").result
            return str(int((data or {}).get("now") or 0) or int(time.time()))
        except Exception:
            return str(int(time.time()))

    async def _start_live(self, room_id: int, area_id: int) -> dict:
        """以直播姬通道形态调 startLive；B站要求人脸认证时抛 FaceAuthRequiredError。"""
        data = {"appkey": START_APPKEY, "area_v2": area_id,
                "build": START_CLIENT["build"], "csrf": self.credential.bili_jct,
                "platform": "pc_link", "room_id": room_id,
                "ts": await self._server_ts(), "version": START_CLIENT["version"]}
        data["sign"] = _app_sign(data)
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                return await Api(url=START_LIVE_URL, method="POST", verify=True,
                                 no_csrf=True, credential=self.credential).update_data(
                    **data).result
        except ResponseCodeException as exc:
            if int(exc.code or 0) == 60024:
                qr = str(((exc.raw or {}).get("data") or {}).get("qr") or "")
                raise FaceAuthRequiredError(
                    "开播需要人脸认证：请用B站App扫描界面上的二维码完成验证，"
                    "验证通过后再点开始直播", qr) from None
            raise _friendly(exc) from None
        except Exception as exc:
            raise _friendly(exc) from None

    async def begin(self, title: str, area_id: int) -> dict:
        """用户点击开播后调用：改标题 -> 开播，仅内存返回 {room_id, url}。"""
        if not str(title or "").strip():
            raise PlatformError("直播标题不能为空")
        if self.credential.bili_jct is None:
            raise PlatformError("Cookie 缺少 bili_jct 字段，无法执行开播，请重新扫码登录")
        info = await self.account()  # 确认当前账号本人的房间状态
        if not info.get("logged_in"):
            raise PlatformError("尚未登录 B站主播账号，请先扫码登录")
        if int(info.get("live_status") or 0) == 1:
            raise PlatformError("检测到该账号已有正在进行的直播，"
                                "本工具不会接管或停止已有推流，请先人工结束当前直播")
        room_id = int(info.get("room_id") or 0)
        if not room_id:
            raise PlatformError(info.get("error") or NO_ROOM_HINT)
        self.room = LiveRoom(room_id, credential=self.credential)
        await self._update_title(room_id, str(title).strip())
        resp = await self._start_live(room_id, int(area_id))
        self.started_room_id = room_id  # start 成功即登记，回滚只针对此房间
        data = resp if isinstance(resp, dict) else {}
        if data.get("need_face_auth") or data.get("need_auth"):
            # 不篡改验证字段、不绕道其他平台：关回自己刚开的房间后交人工处理
            await self._stop_started_room()
            raise FaceAuthRequiredError(
                "开播被要求人脸认证：请用B站App扫描界面上的二维码完成验证后重试",
                str(data.get("qr") or ""))
        rtmp = data.get("rtmp") or {}
        addr = str(rtmp.get("addr") or rtmp.get("url") or "")
        key = str(rtmp.get("code") or rtmp.get("key") or "")
        try:
            url = _validate_push_url(addr, key)
        except PlatformError:
            await self._stop_started_room()
            raise
        return {"room_id": room_id, "url": url}

    async def _session(self):
        from bili import make_session
        return make_session(self.cookie)

    async def update_cover(self, data: bytes, filename: str, mime: str) -> dict:
        if self.credential.bili_jct is None or not self.cookie:
            raise PlatformError("请先扫码登录主播账号，再修改直播间封面")
        session = await self._session()
        try:
            url = await _upload_cover_picture(data, mime, self.credential)
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if not (host == "hdslb.com" or host.endswith(".hdslb.com")):
                raise PlatformError("B站图床返回了意料之外的地址，已放弃更新封面")
            async with asyncio.timeout(TIMEOUT_SECONDS):
                async with session.post(PRE_LIVE_INFO_URL, data={
                        "platform": "web", "mobi_app": "web", "build": "1",
                        "cover": url, "csrf": self.credential.bili_jct,
                        "csrf_token": self.credential.bili_jct},
                        headers={"Referer": "https://live.bilibili.com/"}) as response:
                    result = await response.json(content_type=None)
            if int(result.get("code") or 0) != 0:
                raise PlatformError("B站拒绝更新直播间封面（code=%s）：%s" % (
                    result.get("code"), str(result.get("message") or result.get("msg") or "")[:60]))
            return {"ok": True, "cover": url, "audit": bool((result.get("data") or {}).get("audit_info"))}
        except PlatformError:
            raise
        except Exception as exc:
            raise _friendly(exc) from None
        finally:
            await session.close()

    async def update_notice(self, content: str) -> dict:
        if not self.cookie:
            raise PlatformError("请先扫码登录主播账号，再修改直播间公告")
        content = str(content or "").strip()
        if len(content) > 60:
            raise PlatformError("直播间公告最多 60 个字符")
        try:
            details = await self.account()
            room_id = int(details.get("room_id") or 0)
            if not room_id:
                raise PlatformError(details.get("error") or NO_ROOM_HINT)
            wanted = content
            async with asyncio.timeout(TIMEOUT_SECONDS):
                await LiveRoom(room_id, credential=self.credential).update_news(wanted)
            return {"ok": True, "notice": wanted}
        except PlatformError:
            raise
        except Exception as exc:
            raise _friendly(exc) from None

    async def end(self) -> dict:
        """仅关闭本实例 started_room_id 记录的直播；未开播时幂等返回。"""
        if not self.started_room_id:
            return {"ok": True, "stopped": False}
        room_id = self.started_room_id
        if not await self._stop_started_room():
            raise PlatformError("关闭直播失败，房间号已保留，可稍后重试结束直播")
        return {"ok": True, "stopped": True, "room_id": room_id}

    async def end_external(self, expected_room_id: int) -> dict:
        """用户明确设置的直播姬/OBS 停播；与只清理本实例的 end 分开。"""
        if self.credential.bili_jct is None:
            raise PlatformError("请扫码登录正在直播的主播账号，停播需要完整登录凭证")
        info = await self.account()  # account 会核对直播间 UID 属于当前账号。
        if not info.get("logged_in"):
            raise PlatformError("请先扫码登录正在直播的主播账号")
        room_id = int(info.get("room_id") or 0)
        if not room_id or room_id != int(expected_room_id or 0):
            raise PlatformError("当前主播账号的直播间与定时停播目标不一致，请重新设置")
        if int(info.get("live_status") or 0) != 1:
            return {"ok": True, "stopped": False, "room_id": room_id, "message": "当前直播间未在直播，无需停播"}
        try:
            room = LiveRoom(room_id, credential=self.credential)
            async with asyncio.timeout(TIMEOUT_SECONDS):
                await room.stop()
                for attempt in range(3):
                    details = await room.get_room_info()
                    if int(details["room_info"]["live_status"]) == 0:
                        return {"ok": True, "stopped": True, "room_id": room_id}
                    if attempt < 2:
                        await asyncio.sleep(0.5)
            raise PlatformError("已发送停播请求，但B站仍显示直播中，请检查直播姬是否重新开播")
        except Exception as exc:
            raise _friendly(exc) from None
