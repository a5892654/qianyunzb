import asyncio
import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image

from live_broadcast import Broadcast
import live_platform
from live_platform import BiliBroadcastPlatform, PlatformError

checks, failures = 0, []


def check(ok, label):
    global checks
    checks += 1
    if not ok:
        failures.append(label)
    print(("PASS " if ok else "FAIL ") + label)


def png(width=1280, height=720, mode="RGB"):
    buffer = io.BytesIO()
    Image.new(mode, (width, height), "#336699").save(buffer, "PNG")
    return buffer.getvalue()


def jpg(width=1280, height=720):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "#336699").save(buffer, "JPEG")
    return buffer.getvalue()


def app_stub():
    app = types.SimpleNamespace(pushes=[], cfg={"bili_cookie": "SESSDATA=fake;bili_jct=fake"})
    app.push = lambda kind, data: app.pushes.append((kind, dict(data)))
    return app


def test_image_validation():
    broadcast = Broadcast(app_stub())
    check(broadcast.image_problem(png()) == "", "accept_png_16x9")
    check(broadcast.image_problem(jpg()) == "", "accept_jpg_16x9")
    check("5MB" in broadcast.image_problem(b"x" * (5 * 1024 * 1024 + 1)), "reject_oversize")
    check("有效" in broadcast.image_problem(b"not an image"), "reject_fake_image")
    check(broadcast.image_problem(png(640, 360)) == "", "accept_smallest_allowed")
    check("640" in broadcast.image_problem(png(320, 180)), "reject_small")
    check("比例" in broadcast.image_problem(png(1000, 1000)), "reject_square")
    gif = io.BytesIO()
    Image.new("RGB", (1280, 720)).save(gif, "GIF")
    check("JPG" in broadcast.image_problem(gif.getvalue()), "reject_gif")


async def test_broadcast_bridge():
    app = app_stub()
    broadcast = Broadcast(app)
    platform = Mock()
    platform.update_cover = AsyncMock(return_value={"ok": True, "cover": "https://i0.hdslb.com/x.jpg", "audit": True})
    platform.update_notice = AsyncMock(return_value={"ok": True, "notice": "公告"})
    with patch("live_broadcast.BiliBroadcastPlatform", return_value=platform):
        bad = await broadcast.update_cover(b"junk", "x.jpg", "image/jpeg")
        check(not bad["ok"] and platform.update_cover.await_count == 0, "bad_image_never_uploads")
        ok = await broadcast.update_cover(png(), "cover.png", "image/png")
        check(ok["ok"] and platform.update_cover.await_count == 1, "valid_image_forwards_once")
        platform.update_cover.side_effect = PlatformError("请先扫码登录主播账号")
        denied = await broadcast.update_cover(png(), "cover.png", "image/png")
        check(not denied["ok"] and "登录" in denied["error"], "platform_error_is_safe")
        note = await broadcast.update_notice("今晚八点直播")
        check(note["ok"] and platform.update_notice.await_count == 1, "notice_forwards_once")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def test_platform_cover_flow():
    # 图床上传走 bilibili_api，只替换那一步，验证「先上传 -> 再原样回写 PreLive」与域名闸门。
    session = AsyncMock()
    session.post = Mock(return_value=FakeResponse({"code": 0, "data": {"audit_info": {"a": 1}}}))
    session.close = AsyncMock()
    platform = BiliBroadcastPlatform("SESSDATA=fake;bili_jct=fake;DedeUserID=1")
    platform._session = AsyncMock(return_value=session)
    uploaded = AsyncMock(return_value="https://i0.hdslb.com/bfs/new_dyn/cover.jpg")
    with patch.object(live_platform, "_upload_cover_picture", uploaded):
        result = await platform.update_cover(png(), "cover.png", "image/png")
    check(result["ok"] and result["cover"].startswith("https://i0.hdslb.com/"), "upload_then_prelive")
    check(result["audit"] is True, "audit_flag")
    check(session.post.call_count == 1 and session.close.await_count == 1, "cover_session_closed")
    check(uploaded.await_count == 1 and uploaded.await_args.args[1] == "image/png",
          "upload_gets_real_mime")

    session2 = AsyncMock()
    session2.post = Mock(return_value=FakeResponse({"code": 0, "data": {}}))
    session2.close = AsyncMock()
    platform2 = BiliBroadcastPlatform("SESSDATA=fake;bili_jct=fake")
    platform2._session = AsyncMock(return_value=session2)
    with patch.object(live_platform, "_upload_cover_picture",
                      AsyncMock(return_value="https://evil.example/x.jpg")):
        try:
            await platform2.update_cover(png(), "cover.png", "image/png")
            check(False, "reject_non_hdslb_url")
        except PlatformError as error:
            check("意料之外" in str(error), "reject_non_hdslb_url")
    check(session2.post.call_count == 0, "non_hdslb_url_never_sent")

    session3 = AsyncMock()
    session3.post = Mock(return_value=FakeResponse({"code": -400, "message": "封面不符合规范"}))
    session3.close = AsyncMock()
    platform3 = BiliBroadcastPlatform("SESSDATA=fake;bili_jct=fake")
    platform3._session = AsyncMock(return_value=session3)
    with patch.object(live_platform, "_upload_cover_picture",
                      AsyncMock(return_value="https://i0.hdslb.com/bfs/live/cover.jpg")):
        try:
            await platform3.update_cover(png(), "cover.png", "image/png")
            check(False, "bili_rejection_surfaced")
        except PlatformError as error:
            check("拒绝" in str(error) and "封面不符合规范" in str(error), "bili_rejection_surfaced")

    platform4 = BiliBroadcastPlatform("")
    try:
        await platform4.update_cover(png(), "cover.png", "image/png")
    except PlatformError as error:
        check("登录" in str(error), "anonymous_cover_rejected")
    else:
        check(False, "anonymous_cover_rejected")


class _PictureStub:
    fmt = ""
    url = ""
    error = None

    @classmethod
    def from_content(cls, content, fmt):
        cls.fmt = fmt
        return cls()

    async def upload(self, credential):
        if self.error is not None:
            raise self.error
        return self


async def test_upload_cover_picture():
    with patch.object(live_platform, "PICTURE_TIMEOUT_SECONDS", 5.0):
        _PictureStub.url = "http://i0.hdslb.com/bfs/new_dyn/x.jpg"
        _PictureStub.error = None
        with patch.object(live_platform, "Picture", _PictureStub):
            url = await live_platform._upload_cover_picture(png(), "image/png", None)
        check(url == "https://i0.hdslb.com/bfs/new_dyn/x.jpg", "upload_upgrades_http_to_https")
        check(_PictureStub.fmt == "png", "upload_uses_png_format")

        _PictureStub.url = "//i0.hdslb.com/bfs/live/y.jpg"
        with patch.object(live_platform, "Picture", _PictureStub):
            url = await live_platform._upload_cover_picture(png(), "image/jpeg", None)
        check(url == "https://i0.hdslb.com/bfs/live/y.jpg", "upload_adds_scheme")

        _PictureStub.url = ""
        with patch.object(live_platform, "Picture", _PictureStub):
            try:
                await live_platform._upload_cover_picture(png(), "application/octet-stream", None)
                check(False, "upload_without_url_fails")
            except PlatformError as error:
                check("没有返回图片地址" in str(error), "upload_without_url_fails")
        check(_PictureStub.fmt == "jpg", "upload_defaults_to_jpg")

        _PictureStub.error = asyncio.TimeoutError()
        with patch.object(live_platform, "Picture", _PictureStub):
            try:
                await live_platform._upload_cover_picture(png(), "image/png", None)
                check(False, "upload_timeout_message")
            except PlatformError as error:
                check("超时" in str(error), "upload_timeout_message")

        _PictureStub.error = RuntimeError("SESSDATA=secret leaked")
        with patch.object(live_platform, "Picture", _PictureStub):
            try:
                await live_platform._upload_cover_picture(png(), "image/png", None)
                check(False, "upload_error_never_leaks_cookie")
            except PlatformError as error:
                check("封面上传失败" in str(error) and "secret" not in str(error),
                      "upload_error_never_leaks_cookie")


async def test_platform_notice():
    platform = BiliBroadcastPlatform("SESSDATA=fake;bili_jct=fake")
    try:
        await platform.update_notice("字" * 61)
    except PlatformError as error:
        check("60" in str(error), "notice_max_60")
    else:
        check(False, "notice_max_60")
    platform.account = AsyncMock(return_value={"ok": True, "room_id": 0, "error": "未开通直播间"})
    try:
        await platform.update_notice("公告")
    except PlatformError as error:
        check("直播间" in str(error), "notice_requires_room")
    else:
        check(False, "notice_requires_room")
    platform.account = AsyncMock(return_value={"ok": True, "room_id": 42, "error": ""})
    room = Mock()
    room.update_news = AsyncMock(return_value={"code": 0})
    with patch("live_platform.LiveRoom", return_value=room):
        ok = await platform.update_notice("  今晚八点直播  ")
    check(ok["ok"] and room.update_news.await_args.args[0] == "今晚八点直播", "notice_trimmed_saved")


async def test_routes():
    from aiohttp.test_utils import TestClient, TestServer
    import main as main_mod
    app = object.__new__(main_mod.App)
    app.cfg = {"bili_cookie": "fake", "songs": {}, "tts": {}, "ai": {}, "audio": {}, "broadcast": {}}
    app.ws, app.history, app.status = set(), [], {}
    app.push = lambda *a, **k: None
    app.bili = types.SimpleNamespace(login_uid=None, login_name="", connected=False, client=None, room_id=0)
    app.broadcast = Mock()
    app.broadcast.update_cover = AsyncMock(return_value={"ok": True, "cover": "https://i0.hdslb.com/x.jpg"})
    app.broadcast.update_notice = AsyncMock(return_value={"ok": True, "notice": "公告"})
    app.broadcast.running = False
    app.broadcast.phase = "idle"
    app.broadcast.status = Mock(return_value={})
    app.schedule = Mock(enabled=False, abort_start=False, stop_requested=False, closing=False)
    client = TestClient(TestServer(main_mod.build_web(app)))
    await client.start_server()
    try:
        form = __import__("aiohttp").FormData()
        form.add_field("cover", png(), filename="cover.png", content_type="image/png")
        no_header = await client.post("/api/broadcast/room/cover", data=form)
        check(no_header.status == 403 and app.broadcast.update_cover.await_count == 0, "cover_requires_header")
        form2 = __import__("aiohttp").FormData()
        form2.add_field("cover", png(), filename="cover.png", content_type="image/png")
        ok = await client.post("/api/broadcast/room/cover", data=form2, headers={"X-BVB": "1"})
        body = await ok.json()
        check(ok.status == 200 and body["ok"] and app.broadcast.update_cover.await_count == 1, "cover_multipart_route")

        form3 = __import__("aiohttp").FormData()
        form3.add_field("cover", b"\x89PNG\r\n" + b"x" * (2 * 1024 * 1024),
                        filename="big.png", content_type="image/png")
        big = await client.post("/api/broadcast/room/cover", data=form3, headers={"X-BVB": "1"})
        big_body = await big.json()
        check(big.status == 200 and big_body["ok"] and app.broadcast.update_cover.await_count == 2,
              "cover_accepts_2mb_body")
        check(main_mod.MAX_BODY_BYTES > 5 * 1024 * 1024, "body_limit_above_cover_cap")
        json_post = await client.post("/api/broadcast/room/cover", json={}, headers={"X-BVB": "1"})
        check(json_post.status == 400, "cover_json_rejected")
        note = await client.post("/api/broadcast/room/notice", json={"notice": "公告"}, headers={"X-BVB": "1"})
        check((await note.json())["ok"], "notice_route")
        bad = await client.post("/api/broadcast/room/notice", json={"notice": 1}, headers={"X-BVB": "1"})
        check(bad.status == 400, "notice_non_text_rejected")
    finally:
        await client.close()


def test_ui_source():
    html = Path("C:/gamedata/wt_src/WhaleTalk--main/web/index.html").read_text(encoding="utf-8")
    check('id="roomCoverFile"' in html and "image/jpeg,image/png" in html, "ui_cover_file_input")
    cover_img = html.split('<img id="roomCoverPreview"')[1].split(">")[0]
    check('referrerpolicy="no-referrer"' in cover_img, "ui_cover_img_bypasses_hotlink_guard")
    check("960×600" not in html and "640×360" in html, "ui_cover_hint_matches_gate")
    check("上传并设为封面" in html and "B站审核" in html, "ui_cover_confirm_and_audit")
    check('id="roomNotice"' in html and 'maxlength="60"' in html, "ui_notice_limited")
    check("confirm(" in html.split("$('#roomCoverSave').onclick")[1][:700], "ui_cover_confirmation_present")
    cover_js = html.split("$('#roomCoverSave').onclick")[1][:1400]
    check("5 * 1024 * 1024" in cover_js, "ui_rejects_oversize_cover_before_upload")
    check("JSON.parse(raw)" in cover_js, "ui_survives_non_json_response")


async def main():
    test_image_validation()
    await test_broadcast_bridge()
    await test_platform_cover_flow()
    await test_upload_cover_picture()
    await test_platform_notice()
    await test_routes()
    test_ui_source()
    print("ROOM_SETTINGS_RESULT " + json.dumps({"checks": checks, "failures": failures}, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
