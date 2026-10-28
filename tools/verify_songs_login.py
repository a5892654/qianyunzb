"""网易云扫码登录回归：本地模拟上游，不读取真实账号、不联网。"""
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import netease
from songs import SongManager


class NetEaseLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.code, self.mode, self.requests = 801, "normal", []

        async def key(request):
            self.requests.append(request)
            if self.mode == "http_error":
                return web.Response(status=503, text="unavailable")
            if self.mode == "bad_json":
                return web.Response(text="not JSON")
            return web.json_response({"code": 200, "unikey": "fixture-key"} if self.mode != "no_key" else {"code": 200})

        async def poll(request):
            self.requests.append(request)
            if self.mode == "redirect":
                response = web.HTTPFound("/confirmed")
                response.set_cookie("MUSIC_U", "redirect-token")
                raise response
            body = {"code": str(self.code)}
            if self.mode == "body_cookie":
                body["cookie"] = "MUSIC_U=body-token; Path=/; HttpOnly; __csrf=csrf-token; Max-Age=100"
            response = web.json_response(body)
            if self.code == 803 and self.mode == "normal":
                response.set_cookie("MUSIC_U", "fixture-token")
                response.set_cookie("__csrf", "csrf-token")
            return response

        async def confirmed(request):
            return web.json_response({"code": 803})

        upstream = web.Application()
        upstream.router.add_get("/api/login/qrcode/unikey", key)
        upstream.router.add_get("/api/login/qrcode/client/login", poll)
        upstream.router.add_get("/confirmed", confirmed)
        self.server = TestServer(upstream)
        await self.server.start_server()
        self.base = patch.object(netease, "BASE", str(self.server.make_url("/")).rstrip("/"))
        self.base.start()
        self.ne = netease.NetEase(lambda: "MUSIC_U=old-expired-token")

    async def asyncTearDown(self):
        self.base.stop()
        await self.server.close()

    async def test_anonymous_key_and_all_scan_states(self):
        key, url = await self.ne.qr_key()
        self.assertEqual(key, "fixture-key")
        self.assertIn("codekey=fixture-key", url)
        for code in (801, 802, 803, 800):
            self.code = code
            actual, msg, cookie = await self.ne.qr_poll(key)
            self.assertEqual(actual, code)
            self.assertTrue(msg)
            self.assertEqual("MUSIC_U=fixture-token" in cookie, code == 803)
        for request in self.requests:
            self.assertNotIn("Cookie", request.headers, "重新登录时不能带旧账号凭证")
            self.assertEqual(request.query["type"], "3")
            self.assertTrue(request.query.get("timestamp"))

    async def test_cookie_body_and_redirect_are_supported(self):
        self.code, self.mode = 803, "body_cookie"
        _, _, cookie = await self.ne.qr_poll("fixture-key")
        self.assertIn("MUSIC_U=body-token", cookie)
        self.assertIn("__csrf=csrf-token", cookie)
        self.assertNotIn("Path=", cookie)
        self.assertNotIn("Max-Age=", cookie)
        self.mode = "redirect"
        self.assertIn("MUSIC_U=redirect-token", (await self.ne.qr_poll("fixture-key"))[2])

    async def test_missing_credentials_and_unknown_status_fail_explicitly(self):
        self.code, self.mode = 803, "no_cookie"
        with self.assertRaisesRegex(RuntimeError, "未返回登录凭证"):
            await self.ne.qr_poll("fixture-key")
        self.code = 502
        with self.assertRaisesRegex(RuntimeError, "状态异常"):
            await self.ne.qr_poll("fixture-key")

    async def test_upstream_failures_have_readable_messages(self):
        for mode in ("http_error", "bad_json", "no_key"):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeError, "网易云"):
                await self.ne.qr_key()

    async def test_transport_timeout_is_reported(self):
        with patch.object(netease.aiohttp, "ClientSession", side_effect=TimeoutError):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                await self.ne.qr_key()


class SongLoginRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = types.SimpleNamespace(cfg={"songs": {"netease_cookie": ""}}, push=Mock(), player=Mock())
        self.app.player.music_progress.return_value = (0, 0)
        self.app.songs = SongManager(self.app)
        self.ne = self.app.songs.ne
        self.ne.qr_key = AsyncMock(return_value=("first-key", "https://music.163.com/login?codekey=first-key"))
        self.ne.qr_poll = AsyncMock(return_value=(801, "等待扫码", ""))
        self.ne.account = AsyncMock(return_value={"nickname": "测试用户", "vip": 1})
        self.saved = patch.object(main.config, "save")
        self.save = self.saved.start()
        self.client = TestClient(TestServer(main.build_web(self.app)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.saved.stop()

    async def post(self, path, body=None):
        response = await self.client.post("/api/songs/" + path, json=body or {}, headers={"X-BVB": "1"})
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_confirm_persists_once_and_returns_account_without_cookie(self):
        generated = await self.post("qr")
        self.assertTrue(generated["ok"])
        self.assertIn("<svg", generated["svg"])
        for code in (801, 802, 803):
            self.ne.qr_poll.return_value = (code, "登录成功" if code == 803 else "等待确认", "MUSIC_U=fixture-token" if code == 803 else "")
            result = await self.post("qrpoll", {"key": generated["key"]})
            self.assertTrue(result["ok"])
            self.assertEqual(result["code"], code)
        self.assertEqual(result["account"]["nickname"], "测试用户")
        self.assertNotIn("fixture-token", str(result))
        self.assertEqual(self.app.cfg["songs"]["netease_cookie"], "MUSIC_U=fixture-token")
        self.save.assert_called_once()
        self.assertFalse((await self.post("qrpoll", {"key": generated["key"]}))["ok"])
        self.save.assert_called_once()

    async def test_login_errors_are_json_and_never_saved(self):
        self.ne.qr_key.side_effect = RuntimeError("连接网易云超时")
        self.assertIn("超时", (await self.post("qr"))["error"])
        self.ne.qr_key.side_effect = None
        await self.post("qr")
        self.assertFalse((await self.post("qrpoll"))["ok"])
        self.ne.qr_poll.side_effect = RuntimeError("网络异常")
        self.assertIn("网络", (await self.post("qrpoll", {"key": "first-key"}))["error"])
        self.ne.qr_poll.side_effect = None
        self.ne.qr_poll.return_value = (803, "登录成功", "")
        self.assertFalse((await self.post("qrpoll", {"key": "first-key"}))["ok"])
        self.save.assert_not_called()

    async def test_logout_while_confirmation_in_flight_cannot_restore_login(self):
        await self.post("qr")
        started, released = asyncio.Event(), asyncio.Event()

        async def delayed_poll(key):
            started.set()
            await released.wait()
            return 803, "登录成功", "MUSIC_U=stale-token"

        self.ne.qr_poll.side_effect = delayed_poll
        request = asyncio.create_task(self.post("qrpoll", {"key": "first-key"}))
        await started.wait()
        await self.post("cookie", {"cookie": ""})
        released.set()
        self.assertFalse((await request)["ok"])
        self.assertEqual(self.app.cfg["songs"]["netease_cookie"], "")
        self.assertIsNone(self.app.songs.account)
        self.save.assert_called_once()

    async def test_new_qr_invalidates_old_and_missing_key(self):
        await self.post("qr")
        self.ne.qr_key.return_value = ("second-key", "https://music.163.com/login?codekey=second-key")
        await self.post("qr")
        self.assertFalse((await self.post("qrpoll", {"key": "first-key"}))["ok"])
        self.assertFalse((await self.post("qrpoll", {"key": ""}))["ok"])
        self.ne.qr_poll.assert_not_awaited()
        self.assertTrue((await self.post("qrpoll", {"key": "second-key"}))["ok"])

    async def test_delayed_qr_generation_cannot_replace_newer_qr(self):
        started, released = asyncio.Event(), asyncio.Event()

        async def delayed_key():
            started.set()
            await released.wait()
            return "old-key", "https://music.163.com/login?codekey=old-key"

        self.ne.qr_key.side_effect = delayed_key
        old_request = asyncio.create_task(self.post("qr"))
        await started.wait()
        self.ne.qr_key.side_effect = None
        self.ne.qr_key.return_value = ("new-key", "https://music.163.com/login?codekey=new-key")
        self.assertEqual((await self.post("qr"))["key"], "new-key")
        released.set()
        self.assertFalse((await old_request)["ok"])
        self.assertTrue((await self.post("qrpoll", {"key": "new-key"}))["ok"])

    async def test_failed_refresh_clears_old_account_and_distinguishes_network(self):
        self.app.cfg["songs"]["netease_cookie"] = "MUSIC_U=fixture-token"
        self.app.songs.account = {"nickname": "旧账号"}
        self.ne.account.side_effect = RuntimeError("网络断开")
        await self.app.songs.refresh_account()
        self.assertIsNone(self.app.songs.account)
        self.assertIn("网络", self.app.songs.state()["account_error"])
        self.ne.account.side_effect = None
        self.ne.account.return_value = None
        await self.app.songs.refresh_account()
        self.assertIn("失效", self.app.songs.state()["account_error"])

    async def test_late_account_response_cannot_overwrite_logout(self):
        self.app.cfg["songs"]["netease_cookie"] = "MUSIC_U=fixture-token"
        started, released = asyncio.Event(), asyncio.Event()

        async def delayed_account():
            started.set()
            await released.wait()
            return {"nickname": "旧账号", "vip": 0}

        self.ne.account.side_effect = delayed_account
        request = asyncio.create_task(self.app.songs.refresh_account())
        await started.wait()
        await self.post("cookie", {"cookie": ""})
        released.set()
        await request
        self.assertIsNone(self.app.songs.account)


if __name__ == "__main__":
    unittest.main(verbosity=2)
