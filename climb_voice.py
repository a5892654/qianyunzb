"""Generate short live commentary from the climber's current decisions and results."""

import asyncio
from collections import deque

from brain import Brain, similar_reply
import styles


PERSONA = (
    "你是原创游戏的主播大肥鱼，坐在盆里用吸盘杆登山。参考卢本伟直播的节奏："
    "顺风敢吹，突然失误会急，刚翻盘就嘴硬，自嘲时说具体动作。"
    "允许夸张比喻，台词自己现编，避免照搬真人台词或自称真人。"
    "偶尔接弹幕，也会专心念叨操作。少用语气词，别每句都喊，别骂观众。"
)

VOICE_GAP = 3.5


class ClimbPresenter:
    def __init__(self, app):
        self.app = app
        self.brain = Brain()
        self.jobs = {}
        self.drafts = {}
        self.recent = deque(maxlen=12)
        self.last_voice = float("-inf")
        self.retry_at = 0.0

    async def _write(self, cue):
        cfg = self.app.cfg
        ai = cfg["ai"]
        if ai.get("backend") == "openai" and not ai.get("openai_key"):
            raise RuntimeError("AI 接口缺少 API Key")
        local_ai = {**ai, "style": styles.CUSTOM, "persona": PERSONA, "max_chars": 23,
                    "reply_max_chars": 24, "timeout": min(8, float(ai.get("timeout", 30))),
                    "transient_context": True, "disable_thinking": "deepseek" in str(ai.get("openai_model", "")).lower(),
                    "completion_tokens": 96}
        local_cfg = {**cfg, "ai": local_ai, "interaction": {**cfg.get("interaction", {}), "emotional_value": False}}
        told = " / ".join(list(self.recent)[-6:])
        if cue.kind == "operation":
            instruction = "说自己接下来怎么操作，用意图或判断表达，别声称已经跳成。只提最关键的一点。"
        elif cue.kind == "opening":
            instruction = "像开直播时那样用一句原创的夸口或吐槽开场。"
        else:
            instruction = "即兴接住这次实况，用一句短反应，可以拿自己开玩笑。"
        prompt = (f"实况：{cue.fact}\n{instruction}\n"
                  "控制在23字，没发生的结果别当成事实。用新的开头和比喻。")
        if told:
            prompt += f"\n近几句已说过，避开它们的句式和笑点：{told}"
        line, emotion = await self.brain.ai(local_cfg, prompt)
        if not line:
            raise RuntimeError("AI 没有返回解说")
        return line, emotion

    def _collect(self, now):
        for lane, (cue, job) in list(self.jobs.items()):
            if not job.done():
                continue
            del self.jobs[lane]
            if job.cancelled():
                continue
            try:
                text, emotion = job.result()
                self.app.climb_ai_status = ""
            except Exception as error:
                status = "AI 解说暂不可用，请检查 AI 设置"
                if getattr(self.app, "climb_ai_status", "") != status:
                    self.app.push("log", {"level": "warn", "msg": f"{status}（{type(error).__name__}）"})
                self.app.climb_ai_status = status
                self.retry_at = now + 15
                continue
            if text and not similar_reply(text, self.recent) and self.app.climb.voice_remaining(cue) != 0:
                self.drafts[lane] = (cue, text, emotion or cue.emotion)

    def tick(self, now):
        self._collect(now)
        game, pipe = self.app.climb, self.app.pipe
        for lane, (cue, text, emotion) in list(self.drafts.items()):
            remaining = game.voice_remaining(cue)
            if remaining == 0 or similar_reply(text, self.recent):
                del self.drafts[lane]
                continue
            if remaining is None or now - self.last_voice < VOICE_GAP:
                continue
            if lane == "operation":
                if pipe.current or pipe.pending_replies or "reaction" in self.drafts:
                    continue
                if remaining <= 0.65:
                    del self.drafts[lane]
                    continue
                job_id = pipe.narrate(text, emotion, expires_in=remaining - 0.45)
            else:
                job_id = pipe.narrate_fresh(text, emotion, expires_in=remaining)
            del self.drafts[lane]
            if job_id is not None:
                self.recent.append(text)
                self.last_voice = now
        if now < self.retry_at:
            return
        for lane, produce in (("reaction", game.take_voice), ("operation", game.take_operation_voice)):
            if lane in self.jobs or lane in self.drafts:
                continue
            cue = produce()
            if cue is not None:
                self.jobs[lane] = (cue, asyncio.create_task(self._write(cue)))

    def suspend(self):
        self.drafts.clear()
        for _, job in self.jobs.values():
            if not job.done() and not job.cancelling():
                job.cancel()

    async def close(self):
        self.suspend()
        await asyncio.gather(*(job for _, job in self.jobs.values()), return_exceptions=True)
        await self.brain.close()
