import asyncio
import collections
import json
import logging
import os
import random
import re

import aiohttp

import procutil
from config import BASE_DIR

log = logging.getLogger("ai")

RULES = (
    "\n\n【输出规则】每次只输出主播要说出口的一段话，不超过{max_chars}个字；"
    "纯口语中文，不要表情符号、颜文字、Markdown、引号、括号或动作描写；"
    "不要自称AI，不要解释，不要复述规则；观众名字最多提一次；"
    "遇到辱骂、引战、政治、色情或违规内容就俏皮地转移话题。"
)

GIFT_TOKEN_THANKS = "谢谢老板，这能买不少token了！"
GIFT_THANKS_TIERS = (
    (5, 10, "这份支持收到啦，今天的算力口粮又续上了！"),
    (20, 50, "有你撑腰，陪大家玩游戏聊天都更有精神了，给老板比个心！"),
    (100, 200, "这么大一份心意我认真收下啦，也祝老板天天开心、事事顺利，感谢你的陪伴！"),
)


def gift_thanks(ev):
    """按本次（含连击合并）数量或总价值分档，不把免费礼物当成付费收入。"""
    num = max(1, int(ev.get("num") or 1))
    value = float(ev.get("value") or 0) if ev.get("paid") else 0
    donor = f"{ev['name']}老板" if ev.get("name") else "老板"
    intro = f"谢谢{donor}送的{num}个{ev.get('gift') or '礼物'}！{GIFT_TOKEN_THANKS}"
    level = max((i for i, (count, amount, _) in enumerate(GIFT_THANKS_TIERS, 1)
                 if num >= count or value >= amount), default=0)
    extra = "".join(line for _, _, line in GIFT_THANKS_TIERS[:level])
    return intro, extra


def default_qoder_exe():
    return os.path.join(os.path.expanduser("~"), ".qoder", "bin", "qodercli", "qodercli.exe")


async def list_qoder_models(cfg_ai):
    exe = cfg_ai.get("qoder_exe") or default_qoder_exe()
    if not os.path.exists(exe):
        raise RuntimeError(f"找不到 qodercli：{exe}")
    p = await asyncio.create_subprocess_exec(
        exe, "--list-models", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=procutil.clean_env(), creationflags=procutil.CREATE_NO_WINDOW)
    out, err = await asyncio.wait_for(p.communicate(), 60)
    text = out.decode("utf-8", "replace")
    models = []
    for line in text.splitlines():
        name = re.split(r"\s{2,}", line.strip())[0] if line.strip() else ""
        if name and name.upper() != "MODEL" and not name.startswith(("-", "=")):
            models.append(name)
    if not models:
        msg = (err.decode("utf-8", "replace") or text).strip()
        raise RuntimeError(("qodercli 未登录，请运行 qoder_login.bat。" if "login" in msg.lower() else "") + msg[-300:])
    return models


class QoderSession:
    """常驻 qodercli 进程（stream-json 双向流），首句约 3 秒，之后每句约 1.5 秒，并保留对话上下文。"""

    def __init__(self):
        self.proc = None
        self.turns = 0
        self.sig = None
        self.lock = asyncio.Lock()
        self.workdir = os.path.join(BASE_DIR, "qoder_workdir")

    async def close(self):
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(p.wait(), 1)
            except asyncio.TimeoutError:
                p.kill()

    async def _spawn(self, cfg_ai, system_prompt):
        exe = cfg_ai.get("qoder_exe") or default_qoder_exe()
        if not os.path.exists(exe):
            raise RuntimeError(f"找不到 qodercli：{exe}（请先安装 Qoder CLI）")
        os.makedirs(self.workdir, exist_ok=True)
        args = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--max-turns", "1", "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                "--no-session-persistence", "--system-prompt", system_prompt]
        if cfg_ai.get("qoder_model"):
            args += ["-m", cfg_ai["qoder_model"]]
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=self.workdir, env=procutil.clean_env(),
            creationflags=procutil.CREATE_NO_WINDOW, limit=4 * 1024 * 1024)
        procutil.bind_to_app(self.proc.pid)
        self.turns = 0
        log.info("qodercli 会话已启动 pid=%s", self.proc.pid)

    async def warm(self, cfg_ai, system_prompt):
        async with self.lock:
            if self.proc is None or self.proc.returncode is not None:
                await self._spawn(cfg_ai, system_prompt)
                self.sig = (cfg_ai.get("qoder_exe"), cfg_ai.get("qoder_model"), system_prompt)

    async def ask(self, cfg_ai, system_prompt, user_text):
        async with self.lock:
            sig = (cfg_ai.get("qoder_exe"), cfg_ai.get("qoder_model"), system_prompt)
            if (self.proc is None or self.proc.returncode is not None or sig != self.sig
                    or self.turns >= max(1, int(cfg_ai.get("recycle_after", 60)))):
                await self.close()
                await self._spawn(cfg_ai, system_prompt)
                self.sig = sig
            msg = {"type": "user", "message": {"role": "user", "content": user_text}, "parent_tool_use_id": None}
            try:
                self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
                await self.proc.stdin.drain()
                result = await asyncio.wait_for(self._read_result(), float(cfg_ai.get("timeout", 30)))
            except Exception:
                await self.close()
                raise
            self.turns += 1
            return result

    async def _read_result(self):
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                err = (await self.proc.stderr.read()).decode("utf-8", "replace").strip()
                if "login" in err.lower():
                    err = "qodercli 未登录，请先运行 qoder_login.bat 登录。" + err
                raise RuntimeError(err[-500:] or "qodercli 意外退出")
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("type") == "result":
                if m.get("subtype") != "success" or m.get("is_error"):
                    raise RuntimeError(f"Qoder 返回错误：{m.get('subtype')} {m.get('errors') or m.get('result')}")
                return m.get("result") or ""


class OpenAIChat:
    def __init__(self):
        self.history = collections.deque(maxlen=16)

    @staticmethod
    async def list_models(cfg_ai):
        base = cfg_ai["openai_base"].rstrip("/")
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/models", headers={"Authorization": f"Bearer {cfg_ai['openai_key']}"},
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                d = await r.json(content_type=None)
                if r.status != 200:
                    raise RuntimeError(f"API 错误 {r.status}: {str(d)[:300]}")
        return sorted(m["id"] for m in d.get("data", []) if m.get("id"))

    async def ask(self, cfg_ai, system_prompt, user_text):
        base = cfg_ai["openai_base"].rstrip("/")
        msgs = [{"role": "system", "content": system_prompt}, *self.history, {"role": "user", "content": user_text}]
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{base}/chat/completions",
                              headers={"Authorization": f"Bearer {cfg_ai['openai_key']}"},
                              json={"model": cfg_ai["openai_model"], "messages": msgs, "temperature": 0.9,
                                    "max_tokens": max(200, int(cfg_ai.get("max_chars", 40)) * 3)},
                              timeout=aiohttp.ClientTimeout(total=float(cfg_ai.get("timeout", 30)))) as r:
                d = await r.json(content_type=None)
                if r.status != 200:
                    raise RuntimeError(f"API 错误 {r.status}: {str(d)[:300]}")
        text = d["choices"][0]["message"]["content"] or ""
        self.history.extend([{"role": "user", "content": user_text}, {"role": "assistant", "content": text}])
        return text


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def fill(template, ev):
    return template.format_map(_SafeDict(ev))


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
_BRACKETS = re.compile(r"[（(【\[〔*][^）)】\]〕*]{0,20}[）)】\]〕*]")


def clean_reply(text, max_chars):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = _EMOJI.sub("", text)
    text = _BRACKETS.sub("", text)
    text = re.sub(r"[\"“”'‘’「」『』#*_`>|]", "", text)
    text = re.sub(r"[~～]+", "！", text)
    text = re.sub(r"\s*\n\s*", "，", text.strip())
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"[，,]{2,}", "，", text).strip("，, ")
    limit = int(max_chars * 1.5) + 5
    if len(text) > limit:
        cut = max(text.rfind(p, 0, limit) for p in "。！？!?；")
        text = text[:cut + 1] if cut > 5 else text[:limit]
    return text


class Brain:
    def __init__(self):
        self.qoder = QoderSession()
        self.openai = OpenAIChat()

    @staticmethod
    def emotion_names(cfg):
        l2d = cfg.get("live2d", {})
        if not l2d.get("enabled"):
            return []
        return [e["name"] for e in l2d.get("emotions", []) if e.get("name")]

    def system_prompt(self, cfg):
        cfg_ai = cfg["ai"]
        sp = cfg_ai.get("persona", "") + RULES.format(max_chars=cfg_ai.get("max_chars", 40))
        names = self.emotion_names(cfg)
        if names:
            sp += ("\n【表情】在回复的最开头用【】标出你说这句话时的表情，只能从这些里选一个："
                   + "、".join(names) + "。例如：【开心】欢迎欢迎！表情标签不算字数。")
        return sp

    async def ai(self, cfg, prompt):
        """返回 (文本, 表情名或空)。"""
        cfg_ai = cfg["ai"]
        sp = self.system_prompt(cfg)
        if cfg_ai.get("backend") == "openai":
            raw = await self.openai.ask(cfg_ai, sp, prompt)
        else:
            raw = await self.qoder.ask(cfg_ai, sp, prompt)
        raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()
        emotion = ""
        m = re.match(r"^\s*[【\[]([^】\]]{1,8})[】\]]", raw)
        if m:
            emotion = m.group(1).strip()
            raw = raw[m.end():]
        if emotion not in self.emotion_names(cfg):
            emotion = ""
        return clean_reply(raw, int(cfg_ai.get("max_chars", 40))), emotion

    async def reply(self, cfg, ev_cfg, ev, context=""):
        """返回 (文本, 来源, 表情)。AI 失败时自动退回模板。"""
        if ev["type"] == "gift" and ev_cfg.get("tiered_thanks", True):
            return await self._gift_reply(cfg, ev_cfg, ev)
        lines = [l.strip() for l in ev_cfg.get("templates", "").splitlines() if l.strip()]
        template_text = fill(random.choice(lines), ev) if lines else ""
        if ev_cfg.get("mode") == "ai":
            try:
                prompt = fill(ev_cfg.get("ai_prompt", ""), ev)
                if ev["type"] == "danmaku" and cfg.get("interaction", {}).get("enabled", True):
                    prompt += ("\n先回应观众具体说的内容，语气像正在和朋友聊天。合适时顺势追问一个相关的小问题，"
                               "让观众容易继续接话；不用每条都提问，也不要只顾着游戏解说。")
                if context:
                    prompt += ("\n\n【当前直播游戏上下文，仅供参考】\n" + context
                               + "\n你就是正在打这局游戏的主播。优先回应上面的观众留言；"
                               "聊到游戏时结合当前局势，普通闲聊就自然接话，不要只继续解说游戏。")
                text, emotion = await self.ai(cfg, prompt)
                if text:
                    if ev["type"] == "danmaku" and ev_cfg.get("read_original"):
                        text = f"{ev['name']}说：{ev['text']}。{text}"
                    return text, "AI", emotion
            except Exception as e:
                log.warning("AI 生成失败，改用模板：%s", e)
                return template_text, f"模板(AI失败:{str(e)[:80]})", ""
        return template_text, "模板", ""

    async def _gift_reply(self, cfg, ev_cfg, ev):
        intro, extra = gift_thanks(ev)
        fallback = intro + extra
        if not extra or ev_cfg.get("mode") != "ai":
            return fallback, "礼物感谢模板", ""
        # 只给这次礼物的补充感谢放宽字数，普通弹幕仍遵守原来的长度设置。
        max_chars = len(extra) + 20
        gift_cfg = {**cfg, "ai": {**cfg["ai"], "max_chars": max_chars}}
        prompt = fill(ev_cfg.get("ai_prompt", ""), ev)
        prompt += (f"\n【本次礼物感谢】开头由程序固定拼接：{intro}\n"
                   f"请只输出接在开头后面的补充感谢，约{len(extra)}至{max_chars}字。"
                   "不要重复开头、观众名字、礼物数量或token那句话。"
                   f"这次感谢至少要像下面这样丰富，可以自然改写：{extra}"
                   "不要索要更多礼物，不要承诺额外回报。")
        try:
            text, emotion = await self.ai(gift_cfg, prompt)
            # 模型漏写或过于简短时，仍用对应档位的完整感谢词。
            if len(text) >= len(extra) and GIFT_TOKEN_THANKS not in text and not (ev.get("name") and ev["name"] in text):
                return intro + text, "AI", emotion
        except Exception as e:
            log.warning("AI 礼物感谢生成失败，改用分档模板：%s", e)
            return fallback, f"模板(AI失败:{str(e)[:80]})", ""
        return fallback, "礼物感谢模板", ""

    async def warm(self, cfg):
        if cfg["ai"].get("backend") != "openai":
            try:
                await self.qoder.warm(cfg["ai"], self.system_prompt(cfg))
            except Exception as e:
                log.warning("Qoder 预热失败：%s", e)

    async def close(self):
        await self.qoder.close()
