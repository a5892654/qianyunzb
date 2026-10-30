import asyncio
import itertools
import logging
import random
import re
import time

import numpy as np

from brain import fill
from interaction import AudienceInteraction
from supporters import GiftSupporters
from tts import ENV_FPS, envelope
from tts import decode_audio_file

log = logging.getLogger("pipe")

TYPE_NAMES = {"enter": "进房", "follow": "关注", "danmaku": "弹幕", "gift": "礼物", "guard": "上舰",
              "superchat": "醒目留言", "like": "点赞", "manual": "手动", "farewell": "下播告别",
              "narration": "游戏解说", "reaction": "游戏反应", "interaction": "主动互动"}
PRIORITY = {"farewell": -1, "manual": 0, "superchat": 0, "guard": 0, "gift": 1, "danmaku": 2, "follow": 2,
            "enter": 3, "reaction": 3, "like": 4, "interaction": 4, "narration": 5}
MERGE_BY_TYPE = ("enter", "follow", "like")
_seq = itertools.count(1)


def _split(s):
    return [x.strip() for x in re.split(r"[\n,，;；]", s or "") if x.strip()]


def describe(ev):
    t = ev["type"]
    n = ev.get("name", "")
    if t == "danmaku":
        return f"{n}：{ev.get('text', '')}"
    if t in ("manual", "farewell", "narration", "reaction", "interaction"):
        return ev.get("text", "")
    if t == "gift":
        v = f"（{ev['value']}元）" if ev.get("value") else "（免费）"
        return f"{n} 送出 {ev.get('gift')} ×{ev.get('num')}{v}"
    if t == "guard":
        return f"{n} 开通 {ev.get('guard')} ×{ev.get('num')}个月"
    if t == "superchat":
        return f"{n} 醒目留言 ¥{ev.get('price')}：{ev.get('text', '')}"
    return f"{n} {TYPE_NAMES.get(t, t)}"


class Pipeline:
    def __init__(self, app, *, supporters=None):
        self.app = app
        self.supporters = supporters if supporters is not None else GiftSupporters()
        self.jobs = []
        self.has_job = asyncio.Event()
        self.synth_q = asyncio.Queue(maxsize=1)
        self.play_q = asyncio.Queue(maxsize=1)
        self.paused = False
        self.last_seen = {}
        self.merging = {}
        self.current = None
        self._spoken_ids = set()
        self.pending_replies = set()
        self.interaction = AudienceInteraction(self)
        self.tasks = []

    @property
    def cfg(self):
        return self.app.cfg

    def start(self):
        self.tasks = [asyncio.create_task(c()) for c in (self._ticker, self._gen_worker, self._synth_worker, self._play_worker)]

    # ---------- 事件入口 ----------
    def _display_name(self, name):
        f = self.cfg["filter"]
        name = (name or "").strip()
        if f.get("rename_default", True) and (not name or "*" in name or re.fullmatch(r"bili_\d+", name)):
            name = "新朋友"
        return name[: int(f.get("name_max_len", 12))]

    def _reject(self, ev):
        """返回拒绝原因，None 表示通过。"""
        t = ev["type"]
        ec = self.cfg["events"].get(t)
        fixed = ev.get("fixed_rule") is not None
        if not ec or not (ec.get("enabled") or fixed):
            return "该类型未启用"
        f = self.cfg["filter"]
        ignore = _split(f.get("ignore_users"))
        if ev.get("raw_name") in ignore or str(ev.get("uid")) in ignore:
            return "忽略名单"
        if f.get("ignore_owner") and ev.get("uid") and ev.get("uid") == self.app.bili.owner_uid and not ev.get("test"):
            return "主播本人"
        text = ev.get("text", "")
        if any(w in text or w in ev.get("raw_name", "") for w in _split(f.get("blocklist"))):
            return "屏蔽词"
        if t == "danmaku" and not fixed:
            if ec.get("ignore_emoticon") and ev.get("emoticon"):
                return "表情弹幕"
            trig = _split(ec.get("trigger_words"))
            if trig and not any(w in text for w in trig):
                return "未含触发词"
            if not ev.get("test") and random.random() * 100 >= float(ec.get("probability", 100)):
                return "按概率跳过"
        if t == "gift":
            if not ev.get("paid") and not ec.get("thank_free", True):
                return "免费礼物"
        cd = float(ec.get("cooldown_user", 0) or 0)
        if t == "guard":
            cd = 30
        if cd > 0 and not ev.get("test"):
            key = (t, ev.get("uid") or ev.get("raw_name"), ev.get("level"))
            now = time.time()
            if now - self.last_seen.get(key, 0) < cd:
                return "冷却中"
            self.last_seen[key] = now
        return None

    def match_fixed(self, text):
        text = (text or "").strip()
        for rule in self.cfg.get("fixed_replies", []):
            if not rule.get("enabled", True) or not rule.get("keywords", "").strip() or not rule.get("reply", "").strip():
                continue
            mode = rule.get("match", "contains")
            if mode == "regex":
                try:
                    hit = re.search(rule["keywords"], text) is not None
                except re.error:
                    hit = False
            elif mode == "exact":
                hit = text in _split(rule["keywords"])
            else:
                hit = any(k in text for k in _split(rule["keywords"]))
            if hit:
                return rule
        return None

    def detect_emotion(self, text):
        l2d = self.cfg.get("live2d", {})
        if not l2d.get("enabled"):
            return ""
        for emo in l2d.get("emotions", []):
            if any(k in text for k in _split(emo.get("keywords"))):
                return emo.get("name", "")
        return ""

    def on_event(self, ev):
        if ev["type"] == "wishlist":
            self.app.wishlist.on_event(ev)
            return
        ev["raw_name"] = ev.get("name", "")
        ev["name"] = self._display_name(ev.get("name"))
        room = ev.get("room_id") or getattr(self.app.bili, "room_id", None) or self.cfg.get("room_id")
        if ev["type"] == "gift":
            # 记录送礼事实与是否开口感谢无关，暂停或关闭感谢时也能记住。
            self.supporters.record(room, ev)
            self.app.songs.on_gift(ev)
        if ev["type"] == "enter":
            ev["_gift_supporter"] = bool(
                not ev.get("test") and self.cfg["events"]["enter"].get("supporter_welcome", True)
                and self.supporters.contains(room, ev.get("uid")))
        if ev["type"] == "danmaku" and self.app.songs.on_danmaku(ev):
            self.app.push("event", {"type": "danmaku", "tname": "点歌", "desc": describe(ev), "skip": None,
                                    "test": bool(ev.get("test"))})
            return
        if ev["type"] == "danmaku":
            rule = self.match_fixed(ev.get("text"))
            if rule:
                ev["fixed_rule"] = dict(rule)
        reason = "已暂停" if self.paused else self._reject(ev)
        self.app.push("event", {"type": ev["type"], "tname": TYPE_NAMES.get(ev["type"], ev["type"]),
                                "desc": describe(ev) + ("（命中固定回复）" if ev.get("fixed_rule") else ""),
                                "skip": reason, "test": bool(ev.get("test"))})
        if reason:
            return
        self.interaction.on_event(ev)
        t = ev["type"]
        ec = self.cfg["events"][t]
        win = float(ec.get("merge_window", 0) or 0)
        now = time.time()
        if t in MERGE_BY_TYPE and win > 0:
            # 送礼观众按 UID 单独合并，避免把同批普通观众也说成送过礼物。
            key = (t, ev["uid"]) if t == "enter" and ev.get("_gift_supporter") else t
            m = self.merging.get(key)
            if m:
                if ev.get("_gift_supporter"):
                    m["names"] = [ev["name"]]
                elif ev["name"] not in m["names"]:
                    m["names"].append(ev["name"])
            else:
                self.merging[key] = {"ev": dict(ev), "names": [ev["name"]], "deadline": now + win, "first": now}
            return
        if t == "gift" and win > 0:
            key = ("gift", ev.get("uid") or ev["raw_name"], ev.get("gift"))
            m = self.merging.get(key)
            if m:
                m["ev"]["num"] += ev.get("num", 0)
                m["ev"]["value"] = round(m["ev"].get("value", 0) + ev.get("value", 0), 2)
                m["deadline"] = min(now + win, m["first"] + win * 3)
            else:
                self.merging[key] = {"ev": dict(ev), "deadline": now + win, "first": now}
            return
        self._enqueue(ev)

    def _flush_merge(self, key, m):
        ev = m["ev"]
        if "names" in m:
            names = m["names"]
            k = int(self.cfg["events"][ev["type"]].get("max_names", 3))
            shown = names[:k]
            ev["name"] = "、".join(shown[:-1]) + ("和" if len(shown) > 1 else "") + shown[-1]
            if len(names) > k:
                ev["name"] += f"等{len(names)}位朋友"
        if ev["type"] == "gift":
            minv = float(self.cfg["events"]["gift"].get("min_value", 0) or 0)
            if ev.get("paid") and ev.get("value", 0) < minv:
                self.app.push("log", {"level": "info", "msg": f"礼物价值 {ev['value']} 元低于设定的 {minv} 元，不感谢"})
                return
        self._enqueue(ev)

    async def _ticker(self):
        while True:
            await asyncio.sleep(0.2)
            now = time.time()
            for key, m in list(self.merging.items()):
                if now >= m["deadline"]:
                    del self.merging[key]
                    self._flush_merge(key, m)
            self.interaction.tick()

    def _enqueue(self, ev, text=None):
        job = {"id": next(_seq), "type": ev["type"], "ev": ev, "text": text, "ts": time.time(),
               "prio": PRIORITY.get(ev["type"], 3)}
        self.jobs.append(job)
        if ev["type"] not in ("narration", "reaction", "interaction"):
            self.pending_replies.add(job["id"])
        limit = max(1, int(self.cfg["filter"].get("max_queue", 6)))
        while len(self.jobs) > limit:
            victim = max(self.jobs, key=lambda j: (j["prio"], -j["ts"]))
            self.jobs.remove(victim)
            self._mark_done(victim["id"])
            self.app.push("log", {"level": "warn", "msg": f"队列已满，丢弃：{describe(victim['ev'])}"})
        self.has_job.set()
        self._push_queue()
        return job["id"] if job in self.jobs else None

    def say(self, text, emotion="", *, kind="manual"):
        before = set(j["id"] for j in self.jobs)
        self._enqueue({"type": kind, "name": "", "text": text, "emotion": emotion}, text=text)
        for j in self.jobs:
            if j["id"] not in before:
                return j["id"]
        return None

    def narrate(self, text, emotion=""):
        """普通游戏解说排在观众互动后面，保留手动播报的原有优先级。"""
        return self._enqueue({"type": "narration", "name": "", "text": text, "emotion": emotion,
                              "expires_at": time.monotonic() + 20}, text=text)

    def live_context(self):
        provider = getattr(self.app, "live_context", None)
        value = provider() if callable(provider) else ""
        return value if isinstance(value, str) else ""

    def react(self, text, emotion=""):
        """真实变化的短反应优先于闲聊，排队太久就作废，不播过时的惊呼。"""
        return self._enqueue({"type": "reaction", "name": "", "text": text, "emotion": emotion,
                              "expires_at": time.monotonic() + 12}, text=text)

    def play_audio(self, filename, text="心愿单奖励", emotion="开心"):
        """本地 AI 翻唱与普通播报共用队列、音频设备和口型时钟。"""
        before = set(j["id"] for j in self.jobs)
        self._enqueue({"type": "manual", "name": "", "text": text, "emotion": emotion,
                       "audio_file": filename}, text=text)
        return next((j["id"] for j in self.jobs if j["id"] not in before), None)

    def say_job_id(self, text, emotion="", *, farewell=False):
        """把一句话送进合成+播放队列，返回它的 job id（用于等待播完）。"""
        return self.say(text, emotion, kind="farewell" if farewell else "manual")

    def spoken_done(self, job_id) -> bool:
        """这句话是否已播完或被取消；合成/排队中仍然要等。"""
        if job_id is None:
            return True
        return job_id in self._spoken_ids

    def _mark_done(self, job_id):
        if job_id in self.pending_replies:
            self.interaction.replied()
        self.pending_replies.discard(job_id)
        self._spoken_ids.add(job_id)
        if len(self._spoken_ids) > 256:
            self._spoken_ids = set(sorted(self._spoken_ids)[-128:])

    def clear(self):
        for job in self.jobs:
            self._mark_done(job["id"])
        self.jobs.clear()
        self.merging.clear()
        self._push_queue()

    def _push_queue(self):
        self.app.push("queue", {"items": [f"[{TYPE_NAMES.get(j['type'], j['type'])}] {describe(j['ev']) if j['type'] != 'manual' else j['text']}"
                                          for j in sorted(self.jobs, key=lambda j: (j["prio"], j["ts"]))]})

    # ---------- 工作线 ----------
    def _during_farewell(self, item):
        return (getattr(getattr(self.app, "schedule", None), "active_action", "") == "farewell"
                and item["type"] != "farewell")

    @staticmethod
    def _expired(item):
        return time.monotonic() >= item.get("expires_at", float("inf"))

    async def _gen_worker(self):
        while True:
            await self.has_job.wait()
            if not self.jobs:
                self.has_job.clear()
                continue
            job = min(self.jobs, key=lambda j: (j["prio"], j["ts"]))
            self.jobs.remove(job)
            if not self.jobs:
                self.has_job.clear()
            self._push_queue()
            if self._during_farewell(job) or self._expired(job["ev"]):
                self._mark_done(job["id"])
                continue
            max_wait = float(self.cfg["filter"].get("max_wait", 60))
            if job["prio"] == 0:
                max_wait *= 3
            if job["type"] != "farewell" and time.time() - job["ts"] > max_wait:
                self.app.push("log", {"level": "warn", "msg": f"等待超过 {int(max_wait)} 秒，放弃：{describe(job['ev'])}"})
                self._mark_done(job["id"])
                continue
            dt = getattr(self.app, "dt", None)
            dt = dt if dt is not None and dt.running else None
            try:
                emotion = ""
                rule = job["ev"].get("fixed_rule")
                if job["text"] is not None:
                    text, src, emotion = job["text"], TYPE_NAMES[job["type"]], job["ev"].get("emotion", "")
                elif rule:
                    lines = [l.strip() for l in rule["reply"].splitlines() if l.strip()]
                    text, src, emotion = fill(random.choice(lines), job["ev"]), "固定回复", rule.get("emotion", "")
                elif job["type"] == "interaction":
                    text, src, emotion = await self.interaction.reply(job["ev"])
                else:
                    t0 = time.time()
                    context = dt.commentary_context() if dt and job["type"] in ("danmaku", "superchat") else ""
                    cfg, conversation = self.cfg, ""
                    if job["type"] in ("danmaku", "superchat"):
                        cfg = self.interaction.reply_config(job["ev"])
                        conversation = self.interaction.conversation_context(job["ev"])
                    text, src, emotion = await self.app.brain.reply(
                        cfg, self.cfg["events"][job["type"]], job["ev"], context=context, conversation=conversation)
                    src += f" {time.time() - t0:.1f}s"
            except Exception as e:
                log.exception("生成回复失败")
                self.app.push("log", {"level": "error", "msg": f"生成回复失败：{e}"})
                self._mark_done(job["id"])
                continue
            if not text:
                self._mark_done(job["id"])
                continue
            self.interaction.remember_reply(job["ev"], text)
            if dt and dt is getattr(self.app, "dt", None) and dt.running and job["type"] in ("danmaku", "superchat"):
                dt.remember_interaction(job["ev"], text)
            emotion = emotion or self.detect_emotion(text)
            item = {"id": job["id"], "type": job["type"], "text": text, "src": src, "emotion": emotion,
                    "desc": describe(job["ev"]) if job["text"] is None else ""}
            if self._during_farewell(item):
                self._mark_done(item["id"])
                continue
            if job["type"] == "interaction":
                item["interaction_event"] = job["ev"]
            elif job["type"] in ("danmaku", "superchat"):
                item["conversation_event"] = job["ev"]
            if "expires_at" in job["ev"]:
                item["expires_at"] = job["ev"]["expires_at"]
            if job["ev"].get("audio_file"):
                item["audio_file"] = job["ev"]["audio_file"]
                item["src"] = "心愿单音频"
            self.app.push("reply", item)
            await self.synth_q.put(item)

    def _stream_mode(self):
        """流式：边生成边播，首块到位就开口，不用等整句合成完。"""
        t = self.cfg["tts"]
        return bool(t.get("enabled", True) and t.get("qwen_enabled") and t.get("stream_enabled", True))

    async def _synth_worker(self):
        while True:
            item = await self.synth_q.get()
            if self._during_farewell(item) or self._expired(item):
                self._mark_done(item["id"])
                continue
            tts = self.cfg["tts"]
            if item.get("audio_file"):
                try:
                    item["pcm"], item["sr"] = await asyncio.to_thread(decode_audio_file, item["audio_file"])
                except Exception as e:
                    self.app.push("log", {"level": "error", "msg": f"心愿单音频读取失败：{e}"})
                    self._mark_done(item["id"])
                    continue
            elif tts.get("enabled", True) and not self._stream_mode():
                try:
                    t0 = time.time()
                    item["pcm"], item["sr"] = await self.app.engine.synth(tts, item["text"])
                    item["synth_s"] = round(time.time() - t0, 1)
                except Exception as e:
                    self.app.push("log", {"level": "error", "msg": f"语音合成失败：{e}"})
                    item["pcm"] = None
            await self.play_q.put(item)

    def _push_env(self, item, acc, sr, emitted):
        """在已收到的音频上算口型包络，只推新增的帧；口型因此跟着声音一起长。"""
        env = envelope(np.concatenate(acc), sr)
        if len(env) > emitted:
            self.app.push("speak_env", {"id": item["id"], "env": env[emitted:]})
            return len(env)
        return emitted

    async def _play_stream(self, item, loop):
        from dtgame import ttsd
        a = self.cfg["audio"]
        text, dev, vol = item["text"], a.get("device", ""), float(a.get("volume", 80))
        self.app.push("speak", {"text": text, "id": item["id"], "emotion": item.get("emotion", ""),
                               "fps": ENV_FPS, "env": [], "dur": None, "stream": True})
        t0, acc, env_n, sr = time.time(), [], 0, None
        try:
            await loop.run_in_executor(None, self.app.player.begin_stream, dev, vol)
            async for pcm, r in ttsd.synth_stream(text, self.cfg["tts"]):
                sr = r
                if not acc:
                    item["synth_s"] = round(time.time() - t0, 2)  # 流式下这栏显示的是开口延迟
                acc.append(pcm)
                env_n = self._push_env(item, acc, sr, env_n)
                await loop.run_in_executor(None, self.app.player.feed_stream, pcm, r)
        except Exception as e:
            # 引擎没出声就挂了（掉线/显存不够）：退回整句链路，别把直播间留成哑巴
            if acc:
                log.warning("流式语音中途断流：%s", e)
                self.app.push("log", {"level": "warn", "msg": f"流式语音中途断流：{e}"})
                return
            log.warning("流式语音失败，改整句合成：%s", e)
            self.app.push("log", {"level": "warn", "msg": f"流式语音失败，改整句合成：{e}"})
            pcm, psr = await self.app.engine.synth(self.cfg["tts"], text)
            item["pcm"], item["sr"] = pcm, psr
            await self._play_blocking(item, loop)
            return
        finally:
            await loop.run_in_executor(None, self.app.player.end_stream)

    async def _play_blocking(self, item, loop):
        a = self.cfg["audio"]
        pcm = item.get("pcm")
        self.app.push("speak", {"text": item["text"], "id": item["id"], "synth_s": item.get("synth_s"),
                                "emotion": item.get("emotion", ""), "fps": ENV_FPS,
                                "env": envelope(pcm, item["sr"]) if pcm is not None else None,
                                "dur": round(len(pcm) / item["sr"], 2) if pcm is not None else None})
        if pcm is not None:
            await loop.run_in_executor(None, self.app.player.play_blocking, pcm, item["sr"],
                                       a.get("device", ""), float(a.get("volume", 80)))
        else:
            await asyncio.sleep(min(8, 1 + len(item["text"]) * 0.2))

    async def _play_worker(self):
        loop = asyncio.get_running_loop()
        while True:
            item = await self.play_q.get()
            if self._during_farewell(item) or self._expired(item):
                self._mark_done(item["id"])
                continue
            if item["type"] == "interaction":
                if not self.interaction.can_speak(item["interaction_event"]):
                    self._mark_done(item["id"])
                    continue
                self.interaction.spoken(item["text"], item["interaction_event"])
            else:
                self.interaction.remember_spoken(item["text"])
            if item.get("conversation_event"):
                self.interaction.remember_spoken_reply(item["conversation_event"], item["text"])
            self.current = item
            clock = asyncio.create_task(self._voice_clock(item["id"]))
            try:
                if self._stream_mode() and not item.get("audio_file"):
                    await self._play_stream(item, loop)
                else:
                    await self._play_blocking(item, loop)
            except Exception as e:
                self.app.push("log", {"level": "error", "msg": f"播放失败：{e}"})
            finally:
                clock.cancel()
                self.current = None
                self._mark_done(item["id"])
                self.app.push("speak_end", {"id": item["id"]})

    async def _voice_clock(self, item_id):
        """每 0.25 秒报一次「现在声音播到第几秒」，皮套照它对口型。
        浏览器自己按消息到达的墙上时间推会抢跑：首块音频要等 TTS（实测 ~0.4s），
        声卡 latency=high 再压 ~0.18s；中途断流时这个钟也会停住，嘴就不会空跑。"""
        try:
            while True:
                await asyncio.sleep(0.25)
                self.app.push("speak_pos", {"id": item_id, "pos": round(self.app.player.voice_clock(), 3)})
        except asyncio.CancelledError:
            pass
