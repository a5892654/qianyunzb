import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_platform import BiliBroadcastPlatform, PlatformError

checks, failures = 0, []


def check(ok, label):
    global checks
    checks += 1
    if not ok:
        failures.append(label)
    print(("PASS " if ok else "FAIL ") + label)


async def test_begin_keeps_room_settings():
    platform = BiliBroadcastPlatform("SESSDATA=fake;bili_jct=fake;DedeUserID=1")
    platform.account = AsyncMock(return_value={"ok": True, "logged_in": True,
                                               "room_id": 42, "live_status": 0,
                                               "area_id": 283, "area_name": "单机游戏"})
    platform._start_live = AsyncMock(return_value={
        "rtmp": {"addr": "rtmp://example.com/live", "code": "abc"}})
    result = await platform.begin()
    check(result["room_id"] == 42, "begin_returns_room")
    check(platform._start_live.await_args.args == (42, 283), "begin_reuses_current_area")
    check(hasattr(platform, "update_title") and not hasattr(platform, "update_cover")
          and not hasattr(platform, "areas"),
          "only_title_write_method")

    no_area = BiliBroadcastPlatform("SESSDATA=fake;bili_jct=fake;DedeUserID=1")
    no_area.account = AsyncMock(return_value={"ok": True, "logged_in": True,
                                              "room_id": 42, "live_status": 0,
                                              "area_id": 0})
    no_area._start_live = AsyncMock()
    try:
        await no_area.begin()
    except PlatformError as error:
        check("分区" in str(error) and no_area._start_live.await_count == 0, "missing_area_refuses_start")
    else:
        check(False, "missing_area_refuses_start")


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


async def test_update_title_guards():
    platform = BiliBroadcastPlatform("SESSDATA=fake;bili_jct=fake")
    try:
        await platform.update_title("")
    except PlatformError as error:
        check("标题" in str(error), "title_length_guard")
    else:
        check(False, "title_length_guard")
    platform.account = AsyncMock(return_value={"ok": True, "room_id": 0, "error": "未开通直播间"})
    try:
        await platform.update_title("标题")
    except PlatformError as error:
        check("直播间" in str(error), "title_requires_room")
    else:
        check(False, "title_requires_room")


def test_game_title_defaults():
    import config
    titles = config.DEFAULTS["broadcast"]["game_titles"]
    check(titles.get("stardew") == "大胖鱼开播了，来看我自己种田", "stardew_game_title")
    check(titles.get("demontower") == "大胖鱼开播了，来看我被自己的游戏虐", "demontower_game_title")


async def test_routes():
    from aiohttp.test_utils import TestClient, TestServer
    import main as main_mod
    app = object.__new__(main_mod.App)
    app.cfg = {"bili_cookie": "fake", "songs": {}, "tts": {}, "ai": {}, "audio": {}, "broadcast": {}}
    app.ws, app.history, app.status = set(), [], {}
    app.push = lambda *a, **k: None
    app.bili = types.SimpleNamespace(login_uid=None, login_name="", connected=False, client=None, room_id=0)
    app.broadcast = Mock()
    app.broadcast.update_notice = AsyncMock(return_value={"ok": True, "notice": "公告"})
    app.broadcast.running = False
    app.broadcast.phase = "idle"
    app.broadcast.status = Mock(return_value={})
    app.schedule = Mock(enabled=False, abort_start=False, stop_requested=False, closing=False)
    client = TestClient(TestServer(main_mod.build_web(app)))
    await client.start_server()
    try:
        cover = await client.post("/api/broadcast/room/cover", json={}, headers={"X-BVB": "1"})
        check(cover.status == 404, "cover_upload_route_removed")
        note = await client.post("/api/broadcast/room/notice", json={"notice": "公告"}, headers={"X-BVB": "1"})
        check((await note.json())["ok"], "notice_route")
        bad = await client.post("/api/broadcast/room/notice", json={"notice": 1}, headers={"X-BVB": "1"})
        check(bad.status == 400, "notice_non_text_rejected")
    finally:
        await client.close()


def test_ui_source():
    root = Path(__file__).resolve().parents[1]
    html = (root / "web" / "index.html").read_text(encoding="utf-8")
    platform = (root / "live_platform.py").read_text(encoding="utf-8")
    broadcast = (root / "live_broadcast.py").read_text(encoding="utf-8")
    check('id="roomCoverFile"' not in html and 'id="roomCoverSave"' not in html,
          "ui_has_no_cover_upload")
    check('id="roomCoverPreview"' in html and "千音不会修改" in html,
          "ui_cover_is_read_only")
    check('id="broadcastTitle"' not in html, "ui_has_no_room_title_field")
    check('id="broadcastArea"' not in html and "/api/broadcast/areas" not in html,
          "ui_has_no_area_picker")
    check("Room/update" in platform and "def update_title" in platform,
          "platform_updates_title")
    check("UpdatePreLiveInfo" not in platform and "get_area_info" not in platform
          and "update_cover" not in platform,
          "platform_never_changes_area_or_cover")
    check("update_cover" not in platform and "image_problem" not in broadcast,
          "cover_upload_backend_removed")
    check('id="roomNotice"' in html and 'maxlength="60"' in html, "ui_notice_limited")


async def main():
    await test_begin_keeps_room_settings()
    await test_update_title_guards()
    test_game_title_defaults()
    await test_platform_notice()
    await test_routes()
    test_ui_source()
    print("ROOM_SETTINGS_RESULT " + json.dumps({"checks": checks, "failures": failures}, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
