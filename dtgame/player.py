"""让模型玩《恶魔之塔 Zero》的决策循环。

职责：
  1. 读桥接状态 → 渲染成文本
  2. 问模型要一个动作（严格单行 JSON）
  3. 解析、校验、执行
  4. 把台词交给鲸语的语音管线播出来

模型只负责"选什么"，所有动作都要过游戏自己的校验函数，
非法动作原样回给模型让它重选，不会把游戏搞坏。
"""

import asyncio
import json
import logging
import re

import aiohttp

from dtgame.bridge import BridgeError, GameBridge
from dtgame import view

log = logging.getLogger("dtplay")

# 本局最多复活两次（玩家设定）：第三次阵亡就结束本局回大厅开新局。
MAX_REVIVES = 2

_SAY_STRIP = re.compile(r"[\"'“”‘’「」『』【】\[\]{}()（）#*_`>|]")


def _clean_say(text: str, limit: int = 30) -> str:
    """台词要进 TTS，先剥掉引号/括号/标记，再按标点断句裁到口语长度。"""
    text = _SAY_STRIP.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip(" ，,、")
    if len(text) > limit:
        cut = max(text.rfind(p, 0, limit) for p in "。！？!?；，,")
        text = text[:cut] if cut > 4 else text[:limit]
    return text.strip(" ，,、")

SYSTEM = """你在直播玩《恶魔之塔 Zero》——一款类杀戮尖塔的卡牌 Roguelike。

你必须严格按 JSON 输出，只输出一个 JSON 对象，不要代码块、不要解释、不要多余文字。

战斗时可选 action：
  {"action":"play","slot":手牌序号,"target":敌人序号}   出牌，target 可省略（单体牌需给）
  {"action":"end"}                                      结束回合
  {"action":"potion","slot":道具序号,"target":敌人序号}  使用道具，target 可省略
  {"action":"estus"}                                    喝元素瓶回血，"ash":true 喝灰瓶补光焰，都不消耗回合

地图时：
  {"action":"node","col":列,"row":行}

卡牌奖励：
  {"action":"reward","slot":序号}   或   {"action":"reward","skip":true}

篝火（"op" 是子动作）：
  {"action":"campfire","slot":序号}  或  {"action":"campfire","op":"continue"}

商店（"op" 是子动作）：
  {"action":"shop","slot":序号}      或  {"action":"shop","op":"leave"}

灵魂锻造（选中一张卡 → 选强化方向 → 确认，三步都要单独发一条）：
  {"action":"forge","op":"card","slot":卡牌序号}        挑一张要升级的卡
  {"action":"forge","op":"option","slot":强化项序号}    挑一种强化方向
  {"action":"forge","op":"forge"}                       付灵魂，落实这次锻造
  {"action":"forge","op":"back"}                        不锻了，离开这个窗口

事件：
  {"action":"event","slot":序号}

战斗时另有一招：
  {"action":"radiance","target":敌人序号}  放「光焰技艺」；光焰没到奥义阈值别按，
                                          每回合只有 1 次，按早了就把大招窗口烧掉

系统面板（回响之泉/诅咒神殿/决斗场/深渊裂隙/宝箱/地牢/地图弹窗）：
  这些页面会整块挡在地图上，必须先处理掉才能继续走：
  {"action":"overlay","slot":选项序号}
  {"action":"overlay","slot":控件序号,"op":"button"}  点击窗口列出的可用控件
  {"action":"overlay","op":"back"}  仅当状态明确允许关闭/离开时使用
  事件窗口也允许 overlay 操作。选项与控件使用各自的序号，不要混用。
  只操作当前窗口中列出的可用项；失败后依据错误和新状态重选，不要猜坐标或反复撞同一项。
  Boss魂与章节奖励需要选择奖励，不能当成只有继续按钮；选完奖励再前往下一幕。
  设置窗口只选「关闭」，不要改变画质、屏幕适配或放弃本次爬塔。

战斗内弹窗（界面还是战斗，但屏幕上挡着「选择一项 / 弃一张牌 / 抽牌预览」）：
  这时读出来是 dialog，卡已经打出去了，效果要等你选完才结算。
  用 {"action":"overlay","slot":控件序号,"op":"button"} 按窗口列出的「控件 slot」选一项，
  选项文案就是效果本身，照当前能量/格挡/血量/虚血挑最划算的那个。
  这种时候不要再发 play/end，那只会撞同一个窗口。

选角：
  {"action":"character","slot":序号}
  选角色你自己拿主意，按当前阵容和心情直接点一个，不要停下来等确认。
  但观众要看换人：状态里给了「选角提醒」时必须照办，别连着几局点同一个人。

每个 JSON 都要带一个 "say" 字段，是你此刻对着观众说的一句口语中文，
不超过 30 字，像真人打牌时随口念叨，不要念数值、不要解说规则。

手牌后面标 ✕ 的牌当前打不出来，括号里是原因，别选它。
敌人标了 ✕不可选中 的（已阵亡或隐匿）别当目标，系统会自动改成能选的那个。
道具写了「现在用不了：…」的同理，原因没变就别再撞同一个 slot。
本局最多复活两次，第三次阵亡就回大厅开新局，别跟观众承诺无限复活。

决策要点：
- 优先算清能不能挡住这一回合的伤害，再考虑输出。
- 能量有限，别把牌留到死；能斩杀就斩杀。
- 血少时优先进攻或格挡，别贪。
- 挡不住又还有元素瓶就喝，它不消耗回合，瓶子攒到死没意义。
- 不确定时选最稳的一步。
"""


class Player:
    AI_FAIL_LIMIT = 5

    def __init__(self, cfg_ai: dict, bridge: GameBridge, speak=None, log_push=None,
                 narrate: bool = True, pipe=None, emotions=None, chars=(), on_chars=None):
        self.cfg = cfg_ai
        self.bridge = bridge
        self.speak = speak or (lambda text, emotion="": None)
        self.log_push = log_push or (lambda level, msg: None)
        self.narrate = narrate
        self.pipe = pipe
        self.emotions = emotions or (lambda: [])
        self._pending_speech = None
        self.history: list[dict] = []
        self.running = False
        self.turns_taken = 0
        self.fails = 0
        self.error_streak = 0
        self.ai_fails = 0
        self.dead_waits = 0
        self.last_say = ""
        self.chars: list[str] = []   # 最近几局的角色（短名），用来逼她换人
        for x in (chars or []):
            key = self._char_key(x)
            if key and (not self.chars or self.chars[-1] != key):
                self.chars.append(key)
        del self.chars[:-8]
        self.on_chars = on_chars or (lambda names: None)

    def _system_prompt(self) -> str:
        options = [{"name": e["name"], "motion": e.get("motion", ""), "keywords": e.get("keywords", "")}
                   for e in self.emotions() if e.get("name")]
        if not options:
            return SYSTEM
        return SYSTEM + (
            '\n【Live2D 表情与动作】每个决策 JSON 除 say 外，必须带独立的 "emotion" 字段，'
            '值从下面的 name 中原样选择一个，不能为空，不要把标签写进 say。'
            '根据这句话的语气和游戏局势主动选择：得手时开心、拿奖励时期待、受挫时生气、意外时惊讶等。'
            '吃饭、困倦、token 类动作只在台词和情境合适时选，不要在每次出牌时乱吃乱睡；不要机械地固定选同一项。'
            '\n当前皮套的可选项：' + json.dumps(options, ensure_ascii=False)
        )

    # ---------- 模型 ----------
    @staticmethod
    def _char_key(name: str) -> str:
        """角色名归一：选角界面给「奈尔」，局内状态给「龙枪骑士·奈尔」，得算同一个人。"""
        return str(name or "").split("·")[-1].strip()

    def _remember_character(self, state: dict) -> None:
        """记下每局用的角色：模型没有跨局记忆，不记就会一直点同一个。"""
        name = self._char_key((state.get("run") or {}).get("character"))
        if name and (not self.chars or self.chars[-1] != name):
            self.chars.append(name)
            del self.chars[:-8]
            self.on_chars(list(self.chars))

    def _rotation(self, state: dict) -> str:
        roster = [str(c.get("name") or "").strip()
                  for c in (state.get("character_select") or {}).get("characters") or []]
        roster = [n for n in roster if n]
        if not roster:
            return ""
        used = self.chars[-4:]
        if not used:
            return "选角提醒：这是第一次记录，从名单里挑一个你有话说的角色。"
        fresh = [n for n in roster if self._char_key(n) not in self.chars]
        tip = (f"选角提醒：最近几局已经用过 {'、'.join(used)}，这次必须换一个，"
               f"别再点上一局那个。对着观众说一句为什么换她。")
        if fresh:
            tip += " 还没上过的：" + "、".join(fresh) + "，优先从这里挑。"
        return tip

    def _swap_if_repeat(self, state: dict, slot: int, cid: str) -> int:
        """提醒说了还不听就换：连着点上一局那个角色，观众看到的就是一个人一直打。"""
        chars = (state.get("character_select") or {}).get("characters") or []
        if not chars or not self.chars:
            return slot
        picked = next((c for c in chars
                       if (cid and str(c.get("id", "")) == cid)
                       or int(c.get("slot", -1)) == slot), None)
        name = self._char_key((picked or {}).get("name"))
        if not name or name != self.chars[-1]:
            return slot
        fresh = [c for c in chars
                 if self._char_key(c.get("name")) and self._char_key(c.get("name")) not in self.chars]
        if not fresh:
            return slot
        self.log_push("info", f"AI 又想点上一局的{name}，这一局换成{fresh[0]['name']}")
        return int(fresh[0].get("slot", slot))

    async def _ask(self, state_text: str) -> dict:
        base = str(self.cfg.get("openai_base", "")).rstrip("/")
        if not base or not self.cfg.get("openai_model"):
            raise RuntimeError("未配置 OpenAI 兼容接口（base / model）")
        msgs = [{"role": "system", "content": self._system_prompt()}]
        msgs.extend(self.history[-6:])
        msgs.append({"role": "user", "content": state_text})
        payload = {
            "model": self.cfg["openai_model"],
            "messages": msgs,
            "temperature": float(self.cfg.get("dt_temperature", 0.6)),
            "max_tokens": int(self.cfg.get("dt_max_tokens", 220)),
        }
        headers = {"Authorization": f"Bearer {self.cfg.get('openai_key', '')}"}
        timeout = aiohttp.ClientTimeout(total=float(self.cfg.get("timeout", 60)))
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{base}/chat/completions", headers=headers, json=payload) as r:
                raw = await r.text()
                if r.status != 200:
                    raise RuntimeError(f"API {r.status}: {raw[:200]}")
                try:
                    data = json.loads(raw)
                except ValueError as e:
                    raise RuntimeError(f"API 返回的不是 JSON：{raw[:200]}") from e
        text = (data["choices"][0]["message"]["content"] or "").strip()
        self.history.extend([
            {"role": "user", "content": state_text[-2000:]},
            {"role": "assistant", "content": text[:400]},
        ])
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> dict:
        raw = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        m = re.search(r"\{[^{}]*\}", raw, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                pass
        raise RuntimeError(f"模型没给出合法 JSON：{text[:200]}")

    # ---------- 执行 ----------
    def _do(self, state: dict, act: dict) -> dict:
        screen = state.get("screen")
        a = str(act.get("action", ""))
        try:
            if screen == "battle":
                if a == "play":
                    return self.bridge.play_card(int(act.get("slot", -1)), str(act.get("id", "")),
                                                 int(act.get("target", -1)))
                if a == "end":
                    return self.bridge.end_turn()
                if a == "potion":
                    return self.bridge.use_potion(int(act.get("slot", -1)), int(act.get("target", -1)))
                if a == "radiance":
                    return self.bridge.use_radiance_art(int(act.get("target", -1)))
                if a == "estus":
                    return self.bridge.use_estus(bool(act.get("ash", False)))
            elif screen == "map" and a == "node":
                return self.bridge.choose_node(int(act.get("col", -1)), int(act.get("row", -1)))
            elif screen == "reward" and a == "reward":
                return self.bridge.pick_reward(int(act.get("slot", -1)), bool(act.get("skip", False)))
            elif screen == "campfire" and a == "campfire":
                return self.bridge.campfire(str(act.get("op", "")), int(act.get("slot", -1)))
            elif screen == "shop" and a == "shop":
                return self.bridge.shop(str(act.get("op", "buy")), int(act.get("slot", -1)))
            elif screen == "soul_forge" and a == "forge":
                return self.bridge.soul_forge(str(act.get("op", "")), int(act.get("slot", -1)))
            elif screen == "event" and a == "event":
                body = state[screen]
                return self.bridge.event(int(act.get("slot", -1)), body["window_id"], body["revision"])
            elif screen in (*view.OVERLAY_SCREENS, "dialog", "event") and a == "overlay":
                body = state[screen]
                return self.bridge.overlay(int(act.get("slot", -1)), str(act.get("op", "")),
                                           body["window_id"], body["revision"])
            elif screen == "character_select" and a == "character":
                slot = self._swap_if_repeat(state, int(act.get("slot", -1)),
                                            str(act.get("id", "")))
                return self.bridge.pick_character(slot, str(act.get("id", "")))
        except (BridgeError, ValueError, TypeError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"界面 {screen} 不支持动作 {a}"}

    def _boot(self) -> str:
        """主菜单/大厅这类没有选择的界面，由代码直接推进，不消耗模型调用。"""
        sc = self.bridge.screen().get("screen")
        if sc == "menu":
            self.bridge.call("enter_hall", timeout=30)
            return "从主菜单进入出征大厅"
        if sc == "hall":
            r = self.bridge.call("begin_expedition", timeout=60)
            return "从大厅出征" if r.get("ok") else f"出征失败：{r.get('error', '')}"
        return ""

    async def _wait_spoken(self, job_id, timeout: float = 40.0):
        """等这句话播完（含合成排队时间）。超时就放行，不让一句话卡死整局。"""
        is_done = getattr(self.pipe, "spoken_done", None) if self.pipe else None
        if is_done is None:
            await asyncio.sleep(min(6.0, 0.8 + 0.12 * len(self.last_say)))
            return
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if is_done(job_id):
                return
            await asyncio.sleep(0.15)

    async def step(self) -> dict:
        """走一步：读状态、问模型、执行。返回 {screen, action, ok, say}。"""
        state = await asyncio.to_thread(self.bridge.state)
        self._remember_character(state)
        screen = state.get("screen")
        if state.get("busy"):
            return {"screen": screen, "ok": True, "action": {"action": "wait"}, "say": ""}
        if screen in (None, "menu"):
            return {"screen": screen, "ok": False, "error": "游戏不在可操作界面"}

        battle = state.get("battle") or {}
        if battle.get("awaiting_confirm"):
            r = await asyncio.to_thread(self.bridge.call, "confirm_result", {}, 30)
            return {"screen": screen, "action": {"action": "confirm_result"},
                    "ok": bool(r.get("ok")), "error": r.get("error", ""), "say": ""}
        if screen == "death_analysis":
            r = await asyncio.to_thread(self.bridge.confirm_death_analysis)
            return {"screen": screen, "action": {"action": "confirm_death_analysis"},
                    "ok": bool(r.get("ok")), "error": r.get("error", ""), "say": ""}
        # 搜刮页也是纯「继续」，通关后即进奖励选牌。
        if screen == "loot":
            r = await asyncio.to_thread(self.bridge.close_loot, True)
            return {"screen": screen, "action": {"action": "close_loot"},
                    "ok": bool(r.get("ok")), "error": r.get("error", ""), "say": ""}
        # 死亡面板：本局前两次阵亡从篝火复活，第三次结束本局回大厅开新局。
        if screen == "death":
            revives = int((state.get("run") or {}).get("revives", 0))
            if revives < MAX_REVIVES:
                r = await asyncio.to_thread(self.bridge.revive)
                action = "revive"
            else:
                r = await asyncio.to_thread(self.bridge.return_to_hall)
                action = "return_to_hall"
            return {"screen": screen, "action": {"action": action},
                    "ok": bool(r.get("ok")), "error": r.get("error", ""), "say": ""}
        # 本局结算页：「返回大厅」在危险词名单里，普通窗口读出来是空按钮列表，
        # 模型根本没有可点的东西，只能由代码按下去，然后 _boot 会接着开下一局。
        if screen == "run_end":
            r = await asyncio.to_thread(self.bridge.return_to_hall)
            return {"screen": screen, "action": {"action": "return_to_hall"},
                    "ok": bool(r.get("ok")), "error": r.get("error", ""), "say": ""}
        # 已阵亡但结算面板还没挂出来：等它自己走结算流程，别继续发命令。
        # 但不能干等——面板可能压根不会来（被其它分支接管），超时就强制点一次。
        if screen == "battle" and battle.get("dead"):
            self.dead_waits += 1
            if self.dead_waits < 12:
                await asyncio.sleep(0.5)
                return {"screen": screen, "ok": True, "action": {"action": "wait_death"}, "say": ""}
            self.dead_waits = 0
            r = await asyncio.to_thread(self.bridge.call, "confirm_result", {}, 30)
            return {"screen": screen, "action": {"action": "confirm_result"},
                    "ok": bool(r.get("ok")), "error": r.get("error", ""), "say": ""}

        text = view.render(state)
        if screen == "character_select":
            text += "\n" + self._rotation(state)
        try:
            act = await self._ask(text)
        except Exception as e:
            self.ai_fails += 1
            msg = str(e)
            log.warning("模型决策失败（%d/%d）：%s", self.ai_fails, self.AI_FAIL_LIMIT, msg)
            if self.ai_fails >= self.AI_FAIL_LIMIT:
                self.log_push("error", f"模型连续失败 {self.ai_fails} 次，已停止代打：{msg}")
                self.running = False
            return {"screen": screen, "ok": False, "error": msg, "idle": True}
        self.ai_fails = 0

        # 模型思考期间界面可能已经换了（比如上一张卡选完奖励页自己关了），
        # 这时旧决策必然作废——先复查，别把过期动作发下去。
        # None 和空串都算「没有窗口」，两次读法少给一个字段不该把动作全判废。
        now = await asyncio.to_thread(self.bridge.screen)
        if now.get("screen") != screen or (now.get("window_id") or "") != (state.get("window_id") or ""):
            return {"screen": screen, "action": act, "ok": False,
                    "error": "", "say": "", "stale": True}

        say = str(act.pop("say", "") or "").strip()
        say = _clean_say(say)
        emotion = str(act.pop("emotion", "") or "").strip()
        allowed_emotions = {e["name"] for e in self.emotions() if e.get("name")}
        if emotion not in allowed_emotions:
            emotion = ""
            if allowed_emotions and say:
                self.log_push("warn", "代打未选择有效皮套情绪，本句将按台词关键词匹配")

        # 先出牌再说话：画面是"因"，台词是"果"。
        # 反过来的话（之前就是）声音还在合成，牌早打完了，观感就是音画脱节。
        result = await asyncio.to_thread(self._do, state, act)
        if result.get("stale"):
            return {"screen": screen, "action": act, "ok": False, "stale": True, "say": ""}
        self.history.append({"role": "user", "content":
                             f"实际操作结果：{act}，ok={bool(result.get('ok'))}，"
                             f"error={result.get('error', '')}，screen_after={result.get('screen_after', screen)}"})
        del self.history[:-18]

        if result.get("ok") and say and say != self.last_say:
            self.last_say = say
            if self.narrate:
                try:
                    job = self.speak(say, emotion)
                    if job is not None:
                        # 流式下 ~0.4s 就开口，但模型思考下一手也要 2~3 秒，
                        # 所以这里不阻塞等待——让播报和下一步决策并行。
                        # 真正的节流放在 run_forever 里：下一步动作前才等这句播完。
                        self._pending_speech = job
                except Exception as e:
                    log.warning("播报失败：%s", e)
        if not result.get("ok"):
            err = result.get("error", "")
            self.log_push("warn", f"[{screen}] {act} 被拒：{err}")
            # 战斗已结束/已阵亡时不要盲目补发命令，交给主循环按新状态推进。
            if result.get("battle_over"):
                self.fails = 0
                return {"screen": screen, "action": act, "ok": False, "error": err, "say": say}
            self.fails += 1
            if screen == "battle" and self.fails >= 2:
                # 牌没了/能量不够时反复撞同一张打不出的牌没有意义，把主动权交回去。
                self.fails = 0
                await asyncio.to_thread(self.bridge.end_turn)
            elif self.fails >= 3:
                # 非战斗界面没有「回合」可以交还，只能停下来让人看一眼，
                # 否则会以极高频率重复同一个无效动作。
                self.fails = 0
                self.error_streak += 1
                msg = f"{screen} 界面连续操作失败：{err}"
                self.log_push("error", msg)
                if self.error_streak >= 3:
                    self.log_push("error", "自动打牌已暂停：请检查模型配置或游戏是否卡住")
                    self.running = False
                    return {"screen": screen, "action": act, "ok": False, "error": msg, "say": say}
                await asyncio.sleep(5.0)
        else:
            self.fails = 0
            self.error_streak = 0
            self.turns_taken += 1
        return {"screen": screen, "action": act, "ok": bool(result.get("ok")),
                "error": result.get("error", ""), "say": say,
                "closed": result.get("closed", False), "screen_after": result.get("screen_after", screen)}

    async def run_forever(self, interval: float = 0.4):
        self.running = True
        self.log_push("info", "自动打牌已启动")
        last_screen = None
        while self.running:
            try:
                sc = await asyncio.to_thread(self.bridge.screen)
                if sc.get("busy"):
                    await asyncio.sleep(0.3)
                    continue
                screen = sc.get("screen")
                if screen != last_screen:
                    last_screen = screen
                    self.log_push("info", f"进入界面：{screen}")
                if screen in ("menu", "hall", "other"):
                    msg = await asyncio.to_thread(self._boot)
                    if msg:
                        self.log_push("info", msg)
                    await asyncio.sleep(1.0)
                    continue
                # 上一句还没播完就先等：音画同步靠这里兜住，而不是在 step 里傻等。
                if self._pending_speech is not None:
                    await self._wait_spoken(self._pending_speech)
                    self._pending_speech = None
                r = await self.step()
                if r.get("error") and not r.get("action"):
                    await asyncio.sleep(1.0)
                await asyncio.sleep(interval)
            except BridgeError as e:
                self.log_push("error", str(e))
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("自动打牌循环异常")
                self.log_push("error", f"循环异常：{e}")
                await asyncio.sleep(1.5)
        self.log_push("info", "自动打牌已停止")

    def stop(self):
        self.running = False
