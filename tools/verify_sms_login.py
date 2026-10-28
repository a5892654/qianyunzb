r"""手机验证码登录离线验证：不联网、不真发一条短信。

覆盖：
  A. bili.py 的纯函数（请求参数、错误码文案、响应解析）。
  B. main.py 的 /api/bili/sms/* 路由（参数校验、直播中拒绝、成功后才落盘 Cookie）。

运行：.venv\Scripts\python.exe -m unittest tools.verify_sms_login
"""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bili
from bili import (COOKIE_KEYS, login_params, parse_captcha, parse_login, parse_send,
                  send_params, sms_error)


class ParamsTests(unittest.TestCase):
    def test_send_params_use_web_channel_field_names(self):
        p = send_params("13800001111", {"validate": "V", "seccode": "S"}, "TOKEN", "CH")
        self.assertEqual(p["source"], "main-fe-header")
        self.assertEqual(p["cid"], "86")
        self.assertEqual(p["tel"], "13800001111")
        self.assertEqual((p["token"], p["challenge"]), ("TOKEN", "CH"))
        self.assertEqual((p["validate"], p["seccode"]), ("V", "S"))

    def test_login_params_keep_session_and_captcha_key(self):
        p = login_params("13800001111", "1234", "CKEY")
        self.assertEqual(p["code"], "1234")
        self.assertEqual(p["captcha_key"], "CKEY")
        self.assertEqual(p["source"], "main_web")
        self.assertEqual(p["keep"], "true")

    def test_missing_gee_result_sends_empty_strings(self):
        p = send_params("138", {}, "", "")
        self.assertEqual((p["validate"], p["seccode"]), ("", ""), "缺字段也不能漏键，B站按表单校验")


class ErrorTextTests(unittest.TestCase):
    def test_known_codes_get_plain_chinese(self):
        for code, word in ((89, "频繁"), (-412, "风控"), (5, "验证码"), (100003, "没有注册")):
            self.assertIn(word, sms_error({"code": code, "message": "x"}), f"code={code}")

    def test_unknown_code_falls_back_to_bilibili_message(self):
        self.assertEqual(sms_error({"code": 777, "message": "B站自己的话"}), "B站自己的话")


class ParseTests(unittest.TestCase):
    def test_captcha_returns_geetest_fields(self):
        j = {"code": 0, "data": {"type": "geetest", "token": "T",
                                 "geetest": {"gt": "G", "challenge": "C"}}}
        self.assertEqual(parse_captcha(j), {"gt": "G", "challenge": "C", "token": "T"})

    def test_other_captcha_vendor_tells_user_to_scan(self):
        with self.assertRaises(RuntimeError) as e:
            parse_captcha({"code": 0, "data": {"type": "tencent", "tencent": {"appid": "a"}}})
        self.assertIn("扫码", str(e.exception))

    def test_captcha_error_code_raises(self):
        with self.assertRaises(RuntimeError):
            parse_captcha({"code": -412, "message": "prehandle"})

    def test_send_returns_captcha_key(self):
        self.assertEqual(parse_send({"code": 0, "data": {"captcha_key": "K"}}), "K")

    def test_send_without_key_is_an_error(self):
        with self.assertRaises(RuntimeError) as e:
            parse_send({"code": 0, "data": {}})
        self.assertIn("captcha_key", str(e.exception))

    def test_login_builds_cookie_from_whitelist_only(self):
        j = {"code": 0, "data": {"status": 0}}
        cookies = {"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "213067271",
                   "sid": "x", "buvid3": "不该进来", "fingerprint": "也不该进来"}
        out = parse_login(j, cookies)
        for part in out.split("; "):
            self.assertIn(part.split("=")[0], COOKIE_KEYS)
        self.assertIn("SESSDATA=s", out)
        self.assertNotIn("buvid3", out)

    def test_login_needing_extra_check_says_so(self):
        with self.assertRaises(RuntimeError) as e:
            parse_login({"code": 0, "data": {"status": 1}}, {})
        self.assertIn("扫码", str(e.exception))


def make_app():
    from main import App
    app = object.__new__(App)
    app.cfg = {"bili_cookie": "", "room_id": 0, "audio": {}, "ai": {}, "tts": {},
               "dtgame": {}, "live2d": {"enabled": False}, "broadcast": {}, "web_port": 8787}
    app.push = Mock()
    app.schedule = Mock()
    app.broadcast = SimpleNamespace(running=False, phase="idle", login=AsyncMock())
    app.sms = SimpleNamespace(captcha=AsyncMock(return_value={"gt": "G", "challenge": "C", "token": "T"}),
                              send=AsyncMock(return_value="CKEY"),
                              login=AsyncMock(return_value="SESSDATA=s; bili_jct=j"))
    return app


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, app):
        from aiohttp.test_utils import TestClient, TestServer
        import main as main_mod
        c = TestClient(TestServer(main_mod.build_web(app)))
        await c.start_server()
        self.addCleanup(c.close)
        return c

    async def post(self, c, path, body):
        r = await c.post(path, json=body, headers={"X-BVB": "1"})
        return r.status, await r.json()

    async def test_captcha_route_returns_gee_fields(self):
        app = make_app()
        c = await self.client(app)
        status, j = await self.post(c, "/api/bili/sms/captcha", {})
        self.assertEqual((status, j["ok"]), (200, True))
        self.assertEqual(j["gt"], "G")

    async def test_bad_phone_never_reaches_bilibili(self):
        app = make_app()
        c = await self.client(app)
        for bad in ("", "12345", "abc123456", "13800001111x"):
            status, j = await self.post(c, "/api/bili/sms/send", {"phone": bad})
            self.assertFalse(j.get("ok"), f"坏手机号 {bad!r} 竟被接受")
            self.assertEqual(status, 400)
        app.sms.send.assert_not_awaited()

    async def test_send_route_passes_gee_result_through(self):
        app = make_app()
        c = await self.client(app)
        status, j = await self.post(c, "/api/bili/sms/send",
                                    {"phone": "13800001111", "validate": "V", "seccode": "S",
                                     "token": "T", "challenge": "C"})
        self.assertEqual((status, j["captcha_key"]), (200, "CKEY"))
        args = app.sms.send.await_args.args
        self.assertEqual(args, ("13800001111", {"validate": "V", "seccode": "S"}, "T", "C"),
                         "只把极验结果传给 bili，别把手机号以外的表单原样透传")

    async def test_login_saves_cookie_only_on_success(self):
        app = make_app()
        with patch("main.config.save") as save:
            c = await self.client(app)
            status, j = await self.post(c, "/api/bili/sms/login",
                                        {"phone": "13800001111", "code": "1234",
                                         "captcha_key": "CKEY"})
        self.assertEqual((status, j["ok"]), (200, True))
        self.assertEqual(app.cfg["bili_cookie"], "SESSDATA=s; bili_jct=j")
        save.assert_called_once()
        app.schedule.disable_for_account_change.assert_called_once_with()

    async def test_short_code_rejected_before_bilibili(self):
        app = make_app()
        c = await self.client(app)
        for bad in ("", "12", "123456789"):
            status, j = await self.post(c, "/api/bili/sms/login",
                                        {"phone": "13800001111", "code": bad, "captcha_key": "K"})
            self.assertFalse(j.get("ok"), f"坏验证码 {bad!r} 竟被接受")
        app.sms.login.assert_not_awaited()

    async def test_refused_while_broadcasting(self):
        app = make_app()
        app.broadcast.running = True
        c = await self.client(app)
        status, j = await self.post(c, "/api/bili/sms/send", {"phone": "13800001111"})
        self.assertFalse(j["ok"])
        self.assertIn("停止直播", j["error"])
        app.sms.send.assert_not_awaited()

    async def test_error_message_never_leaks_phone(self):
        app = make_app()
        app.sms.send = AsyncMock(side_effect=RuntimeError("发给 13800001111 的验证码错了"))
        c = await self.client(app)
        status, j = await self.post(c, "/api/bili/sms/send", {"phone": "13800001111"})
        self.assertEqual(status, 400)
        self.assertNotIn("13800001111", j["error"])
        self.assertIn("<手机号>", j["error"])


class LiveContractTests(unittest.TestCase):
    """只检查我们代码里写死的地址与库内一致，不发短信。"""

    def test_urls_match_official_endpoints(self):
        self.assertEqual(bili.SMS_URLS["send"],
                         "https://passport.bilibili.com/x/passport-login/web/sms/send")
        self.assertEqual(bili.SMS_URLS["login"],
                         "https://passport.bilibili.com/x/passport-login/web/login/sms")
        self.assertEqual(bili.SMS_URLS["captcha"],
                         "https://passport.bilibili.com/x/passport-login/captcha")

    def test_cookie_keys_cover_what_bilibili_api_needs(self):
        for k in ("SESSDATA", "bili_jct", "DedeUserID"):
            self.assertIn(k, COOKIE_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
