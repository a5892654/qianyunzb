"""按直播间和 UID 记住真实送礼观众，供下次进房欢迎使用。"""
import logging
import os
import sqlite3
from contextlib import closing, contextmanager

from config import BASE_DIR

log = logging.getLogger("supporters")


def _positive_id(value):
    try:
        number = int(value)
        return str(number) if number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


class GiftSupporters:
    def __init__(self, path=None):
        self.path = path if path is not None else os.path.join(BASE_DIR, "gift_supporters.sqlite3")
        self.rooms = {}

    @contextmanager
    def _database(self):
        with closing(sqlite3.connect(self.path, timeout=0.2)) as db:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS supporters "
                           "(room_id TEXT NOT NULL, uid TEXT NOT NULL, PRIMARY KEY (room_id, uid))")
                yield db

    def _members(self, room):
        if room not in self.rooms:
            members = self.rooms[room] = set()
            try:
                with self._database() as db:
                    members.update(row[0] for row in db.execute(
                        "SELECT uid FROM supporters WHERE room_id = ?", (room,)))
            except (OSError, sqlite3.Error) as error:
                log.warning("送礼观众记录读取失败，本次仅使用内存记录：%s", error)
        return self.rooms[room]

    def contains(self, room_id, uid):
        room, uid = _positive_id(room_id), _positive_id(uid)
        return bool(room and uid and uid in self._members(room))

    def record(self, room_id, ev):
        # 模拟礼物、匿名 UID 和空礼物不能成为真实送礼记录。
        if ev.get("test") or ev.get("type") != "gift" or not _positive_id(ev.get("num")):
            return
        room, uid = _positive_id(room_id), _positive_id(ev.get("uid"))
        if not room or not uid:
            return
        members = self._members(room)
        if uid in members:
            return
        members.add(uid)
        try:
            with self._database() as db:
                db.execute("INSERT OR IGNORE INTO supporters (room_id, uid) VALUES (?, ?)", (room, uid))
        except (OSError, sqlite3.Error) as error:
            log.warning("送礼观众记录保存失败，本次仍会专属欢迎：%s", error)
