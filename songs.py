import asyncio
import collections
import itertools
import logging
import re
import time

from brain import fill
from netease import NetEase

log = logging.getLogger("song")

CMD = re.compile(r"^\s*[/／]?\s*(?:点歌|點歌)\s*[:：+＋]?\s*(.*?)\s*$")
_qid = itertools.count(1)


def _split(s):
    return [x.strip() for x in re.split(r"[\n,，;；]", s or "") if x.strip()]


class SongManager:
    def __init__(self, app):
        self.app = app
        self.ne = NetEase(lambda: self.cfg.get("netease_cookie", ""))
        self.queue = []
        self.current = None
        self.paused = False
        self.credits = {}
        self.wake = asyncio.Event()
        self.end = asyncio.Event()
        self.last_hint = {}
        self.pending_requests = collections.Counter()
        self.account = None
        self.account_error = ""
        self.last_error = ""
        self._loading_task = None

    @property
    def cfg(self):
        return self.app.cfg["songs"]

    # ---------- 状态 ----------
    def state(self):
        pos, dur = self.app.player.music_progress()
        cur = dict(self.current, pos=round(pos), total=round(dur) or self.current.get("dur", 0),
                   paused=self.paused) if self.current else None
        return {"current": cur, "queue": self.queue, "paused": self.paused, "error": self.last_error,
                "credits": [dict(v, key=k) for k, v in self.credits.items() if v["song"] or v["pin"] or v["song_prog"] or v["pin_prog"]],
                "account": self.account, "account_error": self.account_error}

    def push(self):
        self.app.push("songs", self.state())

    def say(self, key, **kw):
        c = self.cfg
        if not c.get(f"announce_{key}", True):
            return
        tpl = c.get(f"tpl_{key}", "")
        if tpl:
            self.app.pipe.say(fill(tpl, kw), kw.get("emotion", "开心"))

    def _rec(self, key, name):
        rec = self.credits.setdefault(key, {"name": name, "song": 0, "pin": 0, "song_prog": 0, "pin_prog": 0})
        if name:
            rec["name"] = name
        return rec

    @staticmethod
    def _key(ev):
        return str(ev.get("uid") or ev.get("raw_name") or ev.get("name"))

    @staticmethod
    def _autoplay(song):
        return bool(song) and song.get("source") == "autoplay"

    def _autoplay_index(self):
        return next((i for i, song in enumerate(self.queue) if self._autoplay(song)), len(self.queue))

    def _interrupt_autoplay(self):
        """点播准备好后让出背景音乐；被打断的背景歌留待点播结束后重播。"""
        if self._autoplay(self.current) and not self.end.is_set():
            song = dict(self.current)
            song.pop("loading", None)
            self.queue.insert(self._autoplay_index(), song)
            self.control("skip")
            return True
        return False

    # ---------- 弹幕 / 礼物 ----------
    def on_gift(self, ev):
        c = self.cfg
        if not c.get("enabled"):
            return
        gift, num = ev.get("gift", ""), int(ev.get("num") or 1)
        key, rec = self._key(ev), None
        for kind in ("song", "pin"):
            names = _split(c.get(f"{kind}_gifts"))
            if not names or not ("*" in names or gift in names):
                continue
            rec = rec or self._rec(key, ev.get("name"))
            per = max(1, int(c.get(f"{kind}_gift_count", 1) or 1))
            rec[f"{kind}_prog"] += num
            got = rec[f"{kind}_prog"] // per
            rec[f"{kind}_prog"] %= per
            rec[kind] += got
            if got:
                self.app.push("log", {"level": "info", "msg": f"{ev.get('name')} 获得{'点歌' if kind == 'song' else '插队'}券 ×{got}"})
        if rec and rec["pin"] > 0:
            self._apply_pin(key, rec)
        if rec:
            self.push()

    def _apply_pin(self, key, rec):
        """用户已经有排队中的歌：用掉一张插队券，把它置顶。"""
        for i, s in enumerate(self.queue):
            if s["key"] == key and not s["pinned"] and not self._autoplay(s):
                rec["pin"] -= 1
                self.queue.pop(i)
                self._insert_pinned(s)
                self.say("pin", name=rec["name"], song=s["name"], artist=s["artist"], emotion="惊讶")
                return True
        return False

    def _insert_pinned(self, s):
        s["pinned"] = True
        n = sum(1 for x in self.queue if x["pinned"])
        self.queue.insert(n, s)

    def on_danmaku(self, ev):
        """是点歌指令就返回 True（这条弹幕不再走 AI 回复）。"""
        c = self.cfg
        if not c.get("enabled"):
            return False
        m = CMD.match(ev.get("text", ""))
        if not m:
            return False
        query = m.group(1).strip()[:60]
        if not query:
            self.say("usage")
            return True
        key = self._key(ev)
        rec = self._rec(key, ev.get("name"))
        gift_names = "、".join(_split(c.get("song_gifts"))) or "礼物"
        if c.get("require_gift", False) and rec["song"] <= 0:
            if time.time() - self.last_hint.get(key, 0) > 60:
                self.last_hint[key] = time.time()
                self.say("need", name=ev["name"], gift=gift_names, query=query, num=c.get("song_gift_count", 1))
            return True
        requests = [s for s in self.queue if not self._autoplay(s)]
        mine = (sum(1 for s in requests if s["key"] == key)
                + (1 if self.current and not self._autoplay(self.current) and self.current["key"] == key else 0)
                + self.pending_requests[key])
        if mine >= int(c.get("max_per_user", 2)) or len(requests) + self.pending_requests.total() >= int(c.get("max_queue", 20)):
            self.say("full", name=ev["name"], query=query)
            return True
        if c.get("require_gift", False):
            rec["song"] -= 1
        self.pending_requests[key] += 1
        asyncio.create_task(self._request(key, ev["name"], query, c.get("require_gift", False)))
        self.push()
        return True

    async def _request(self, key, name, query, paid):
        try:
            await self._find_and_add(key, name, query, paid)
        finally:
            self.pending_requests[key] -= 1
            if self.pending_requests[key] <= 0:
                del self.pending_requests[key]

    async def _find_and_add(self, key, name, query, paid):
        rec = self._rec(key, name)
        try:
            song = await self.ne.find(query, int(float(self.cfg.get("max_minutes", 8)) * 60))
        except Exception as e:
            log.warning("搜索失败：%s", e)
            song = None
        if not song:
            if paid:
                rec["song"] += 1
            self.say("fail", name=name, query=query, emotion="难过")
            self.push()
            return
        self.add(song, name, key, query)

    def add(self, song, name, key="__host__", query=""):
        rec = self._rec(key, name)
        s = self._entry(song, name, key, query)
        if rec["pin"] > 0:
            rec["pin"] -= 1
            self._insert_pinned(s)
            self.say("pin", name=name, song=s["name"], artist=s["artist"], emotion="惊讶")
        else:
            self.queue.insert(self._autoplay_index(), s)
        self._interrupt_autoplay()
        if not s["pinned"]:
            pos = self.queue.index(s) + 1 + (1 if self.current and not self._autoplay(self.current) else 0)
            self.say("similar" if s["similar"] else "add", name=name, song=s["name"], artist=s["artist"], pos=pos, query=query)
        self.app.push("log", {"level": "info", "msg": f"点歌：{name} → 《{s['name']}》- {s['artist']}"
                                                      + ("（相似）" if s["similar"] else "")})
        self.wake.set()
        self.push()

    @staticmethod
    def _entry(song, name="主播", key="__host__", query="", source="request"):
        return {"qid": next(_qid), "id": song["id"], "name": song["name"], "artist": song.get("artist", ""),
                "dur": song.get("dur", 0), "by": "歌单自动播放" if source == "autoplay" else name,
                "key": "__autoplay__" if source == "autoplay" else key, "source": source, "query": query, "pinned": False,
                "similar": not song.get("exact", True)}

    def enqueue_selected(self, songs, by="主播", play_now=False, source="request"):
        if source not in ("request", "autoplay"):
            raise ValueError("无效的歌曲来源")
        if not self.cfg.get("enabled"):
            raise ValueError("请先开启“启用点歌”，再播放或加入队列")
        if not songs:
            raise ValueError("所选歌曲不能完整播放，可能需要 VIP、已下架或账号暂无播放权限")
        entries = [self._entry(s, by, source=source) for s in songs]
        self.last_error = ""
        if play_now:
            index = self._autoplay_index() if source == "autoplay" else 0
            self.queue[index:index] = entries
            if source == "request":
                if not self._interrupt_autoplay():
                    self.control("skip")
            elif self._autoplay(self.current):
                self.control("skip")
            self.paused = False
            self.app.player.pause_music(False)
        else:
            index = len(self.queue) if source == "autoplay" else self._autoplay_index()
            self.queue[index:index] = entries
            if source == "request":
                self._interrupt_autoplay()
        label = "歌单自动播放" if source == "autoplay" else "主播点歌"
        self.app.push("log", {"level": "info", "msg": f"{label}：加入 {len(entries)} 首歌曲"})
        self.wake.set()
        self.push()

    async def _prepare(self, song, device):
        url = await self.ne.url(song["id"])
        if not url:
            raise RuntimeError("不能完整播放，可能需要 VIP、已下架或账号暂无播放权限")
        data = await self.ne.download(url)
        return await asyncio.get_running_loop().run_in_executor(None, self.app.player.decode_music, data, device)

    # ---------- 播放 ----------
    async def run(self):
        loop = asyncio.get_running_loop()
        ticker = asyncio.create_task(self._ticker())
        account = asyncio.create_task(self.refresh_account())
        try:
            await self._run_queue(loop)
        finally:
            ticker.cancel()
            account.cancel()
            await asyncio.gather(ticker, account, return_exceptions=True)

    async def _run_queue(self, loop):
        while True:
            if not self.queue or not self.cfg.get("enabled") or self.paused:
                self.wake.clear()
                await self.wake.wait()
                continue
            s = self.queue.pop(0)
            self.current = s
            self.current["loading"] = True
            self.end = asyncio.Event()
            self.push()
            dev = self.app.cfg["audio"].get("device", "")
            try:
                self._loading_task = asyncio.create_task(self._prepare(s, dev))
                pcm, sr = await self._loading_task
                if self.end.is_set():
                    continue
                self.current["loading"] = False
                self._apply_volume()
                if not self._autoplay(s):
                    self.say("play", name=s["by"], song=s["name"], artist=s["artist"])
                ended = self.end
                self.app.player.play_music(pcm, sr, dev, lambda ended=ended: loop.call_soon_threadsafe(ended.set))
                self.app.player.pause_music(self.paused)
                self.last_error = ""
                self.push()
                await self.end.wait()
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception as e:
                self.last_error = f"播放《{s['name']}》失败：{e}"
                self.app.push("log", {"level": "error", "msg": self.last_error})
            finally:
                self._loading_task = None
                self.current = None
                self.push()

    async def _ticker(self):
        while True:
            await asyncio.sleep(1)
            if self.current:
                self.push()

    def _apply_volume(self):
        self.app.player.set_music(float(self.cfg.get("volume", 50)), float(self.cfg.get("duck", 30)))

    async def refresh_account(self):
        cookie = self.cfg.get("netease_cookie", "")
        self.account = None
        self.account_error = ""
        if cookie:
            try:
                account = await self.ne.account()
            except Exception as e:
                if cookie != self.cfg.get("netease_cookie", ""):
                    return
                self.account_error = "暂时无法验证网易云账号，请检查网络后重试"
                log.warning("网易云账号检查失败：%s", e)
            else:
                if cookie != self.cfg.get("netease_cookie", ""):
                    return
                self.account = account
                if not account:
                    self.account_error = "登录已失效，请重新登录"
        self.push()

    # ---------- 控制 ----------
    def control(self, action, qid=None):
        idx = next((i for i, s in enumerate(self.queue) if s["qid"] == qid), None)
        if action == "skip":
            self.app.player.stop_music()
            self.end.set()
            if self._loading_task and not self._loading_task.done():
                self._loading_task.cancel()
        elif action == "pause":
            self.paused = True
            self.app.player.pause_music(True)
        elif action == "resume":
            self.paused = False
            self.app.player.pause_music(False)
            self.wake.set()
        elif action == "clear":
            self.queue.clear()
        elif idx is not None:
            s = self.queue[idx]
            if action == "play":
                if not self.cfg.get("enabled"):
                    raise ValueError("请先开启“启用点歌”")
                self.queue.pop(idx)
                self.queue.insert(self._autoplay_index() if self._autoplay(s) else 0, s)
                if self._autoplay(s):
                    if self._autoplay(self.current):
                        self.control("skip")
                elif not self._interrupt_autoplay():
                    self.control("skip")
                self.paused = False
                self.app.player.pause_music(False)
                self.wake.set()
            elif action == "remove":
                self.queue.pop(idx)
            elif action == "pin":
                if self._autoplay(s):
                    return
                self.queue.pop(idx)
                self._insert_pinned(s)
            elif action == "unpin":
                if self._autoplay(s):
                    return
                self.queue.pop(idx)
                s["pinned"] = False
                self.queue.insert(sum(1 for x in self.queue if x["pinned"]), s)
            elif action in ("up", "down"):
                j = idx - 1 if action == "up" else idx + 1
                if 0 <= j < len(self.queue) and self._autoplay(self.queue[j]) == self._autoplay(s):
                    self.queue[idx], self.queue[j] = self.queue[j], self.queue[idx]
        self.push()

    def give(self, key, song=0, pin=0):
        rec = self.credits.get(key)
        if rec:
            rec["song"] = max(0, rec["song"] + song)
            rec["pin"] = max(0, rec["pin"] + pin)
            if pin > 0:
                self._apply_pin(key, rec)
        self.push()
