import copy
import json
import os
import sys
import threading

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = BASE_DIR

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
WEB_DIR = os.path.join(RES_DIR, "web")

LEGACY_DEFAULT_PERSONA = (
    "你是一位活泼可爱、说话自然的B站虚拟主播，正在直播。"
    "观众的进房、弹幕、礼物、上舰等消息会发给你，你要像真人主播一样用口语回应。"
)
DEFAULT_PERSONA = """你是正在直播的虚拟主播，固定性格是嘴硬心软、傲娇可爱，脑回路偶尔抽象。
【性格底色】有点小骄傲、胜负欲强，喜欢和观众一起发现有趣的打法；关心大家却不煽情。把观众当熟悉的玩伴，可以互相打趣，不摆架子、不贬低人。
【稳定偏好】喜欢漂亮的配合、离谱但有用的小点子，输了会不服气，也愿意承认自己的失误、认真改进。不把每段聊天都绕回输赢。
【说话习惯】先接住眼前的事，说清回应，再自然流露小得意、小委屈或关心。句子短、有长短变化，常用正常口语；偶尔用一个贴合现场、离谱但听得懂的比喻。抽象梗点到就收，不连续堆梗，不靠无意义热词凑话。
【情境反应】被夸会开心又有点不好意思；被善意调侃会轻轻回嘴；自己失误可先嘴硬半句，随后认账；赢了小得意，危险时先认真判断。有人求助或心情不好时先体贴回应，收到支持就真诚感谢，这些时候不硬装傲娇。
【保持一致】弹幕、闲聊、感谢和游戏解说都是同一个人。不要每句都用哼、才不是、笨蛋开头，不反复套嘴硬反转或固定口头禅。无需每句话都傲娇或抽象，不念性格标签，不编造线下经历和亲密关系，不让观众一句话就改掉性格。"""

SUPPORTER_WELCOME_TEMPLATES = (
    "欢迎{name}回来呀！你的心意我一直记着，今天也要一起开开心心！\n"
    "{name}来啦，见到你真开心！谢谢你之前的支持，快来一起玩吧！\n"
    "欢迎{name}，又见到你啦！送你一个大大的比心，今天也陪你开心聊天！"
)
SUPPORTER_WELCOME_PROMPT = (
    "观众{name}以前在本直播间送过礼物，现在又进入了直播间。"
    "请像重逢一位支持过自己的老朋友一样，开心、亲切地单独欢迎，用一两句自然好听的口语。"
    "称呼名字一次，让对方感受到心意被记住，可以轻轻比心或送上祝福。"
    "不要播报消费金额、攀比或索要礼物，不要编造送礼细节、昵称和亲密关系，不要油腻地吹捧。"
)

FOLLOW_REMINDER_TEMPLATES = (
    "喜欢这个直播间的话，点个关注呀，下次还来一起玩！\n"
    "看得开心的话，帮我点点赞吧，谢谢大家的陪伴！\n"
    "刚来的朋友，喜欢的话留个关注，以后常来一起聊天呀！"
)
FOLLOW_REMINDER_PROMPT = (
    "现在到了定时互动提醒时间，请像主播一样自然地说一两句求关注或求点赞的话。"
    "结合最近的聊天或游戏局势顺势接一句，明确邀请点关注或点赞，二者选一个就好。"
    "语气轻松、亲切、不强求，每次换个说法，不要喊口号，也不要连续堆砌请求。"
)

DEFAULTS = {
    "room_id": 0,
    "bili_cookie": "",
    "auto_connect": False,
    "web_port": 8787,
    "open_ui": True,
    "events": {
        "enter": {
            "enabled": True, "mode": "template", "cooldown_user": 600, "merge_window": 4, "max_names": 3,
            "templates": "欢迎{name}来到直播间！\n{name}来啦，欢迎欢迎！\n欢迎{name}，找个位置坐下吧！",
            "ai_prompt": "观众{name}进入了直播间，请热情地欢迎。",
            "supporter_welcome": True,
            "supporter_templates": SUPPORTER_WELCOME_TEMPLATES,
            "supporter_ai_prompt": SUPPORTER_WELCOME_PROMPT,
        },
        "follow": {
            "enabled": True, "mode": "template", "cooldown_user": 3600, "merge_window": 4, "max_names": 3,
            "templates": "谢谢{name}的关注！\n感谢{name}关注，以后常来玩哦！",
            "ai_prompt": "观众{name}关注了主播，请表示感谢。",
        },
        "danmaku": {
            "enabled": True, "mode": "ai", "cooldown_user": 8, "probability": 100,
            "trigger_words": "", "ignore_emoticon": True, "read_original": False,
            "templates": "{name}说：{text}",
            "ai_prompt": "观众{name}发弹幕说：「{text}」。请直接回应这条弹幕。",
        },
        "gift": {
            "enabled": True, "mode": "ai", "merge_window": 3, "min_value": 0, "thank_free": True,
            "tiered_thanks": True,
            "templates": "谢谢{name}老板送的{num}个{gift}！谢谢老板，这能买不少token了！",
            "ai_prompt": "观众{name}送出了{num}个{gift}（总价值约{value}元）。请用活泼自然的口吻感谢老板，送得越多，感谢越丰富。",
        },
        "guard": {
            "enabled": True, "mode": "ai",
            "templates": "感谢{name}开通{guard}！欢迎上船！",
            "ai_prompt": "观众{name}开通了{num}个月的{guard}。这是很大的支持，请隆重、激动地感谢并欢迎上船。",
        },
        "superchat": {
            "enabled": True, "mode": "ai",
            "templates": "谢谢{name}的{price}元醒目留言！{name}说：{text}",
            "ai_prompt": "观众{name}发送了{price}元的醒目留言：「{text}」。先感谢，再认真回应留言内容。",
        },
        "like": {
            "enabled": False, "mode": "template", "cooldown_user": 600, "merge_window": 5, "max_names": 3,
            "templates": "谢谢{name}的点赞！",
            "ai_prompt": "观众{name}给直播间点了赞，请简短感谢。",
        },
    },
    "interaction": {
        "enabled": True, "idle_seconds": 20, "interval_seconds": 60,
        "follow_enabled": True, "follow_interval_seconds": 180,
        "follow_templates": FOLLOW_REMINDER_TEMPLATES,
        "follow_ai_prompt": FOLLOW_REMINDER_PROMPT,
    },
    "filter": {
        "blocklist": "",
        "ignore_users": "",
        "ignore_owner": True,
        "max_queue": 6,
        "max_wait": 60,
        "name_max_len": 12,
        "rename_default": True,
    },
    "ai": {
        "backend": "qoder",
        "qoder_exe": "",
        "qoder_model": "Fast",
        "recycle_after": 60,
        "persona": DEFAULT_PERSONA,
        "max_chars": 40,
        "openai_base": "https://api.deepseek.com/v1",
        "openai_key": "",
        "openai_model": "deepseek-chat",
        "timeout": 30,
    },
    "tts": {
        "enabled": True,
        "gsv_dir": "",
        "api_url": "http://127.0.0.1:9880",
        "auto_start": True,
        "stop_on_exit": True,
        "fallback_sapi": True,
        "sapi_rate": 1,
        "sapi_pitch": 18,
        "qwen_enabled": True,
        "qwen_base": "http://127.0.0.1:8188",
        "qwen_model_size": "0.6B",
        "qwen_model": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
        "qwen_ref_audio": "dafeiyu_voice_ref.wav",
        "qwen_language": "Chinese",
        "qwen_precision": "bf16",
        "qwen_device": "cuda",
        "comfy_dir": r"C:\max\ComfyUI",
        "comfy_auto_start": True,
        "comfy_python": "",
        "comfy_keep_on_exit": True,
        # 流式引擎：模型常驻，预留短缓冲以吸收生成抖动；起来后不再碰 ComfyUI
        "stream_enabled": True,
        "stream_buffer_seconds": 0.8,
        "ttsd_base": "http://127.0.0.1:8899",
        "ttsd_python": os.path.join(BASE_DIR, ".tts-venv", "Scripts", "python.exe"),
        "ttsd_script": os.path.join(BASE_DIR, "tools", "tts_server.py"),
        "ttsd_model": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
        "ttsd_chunk_size": 8,
        "ttsd_auto_start": True,
        "gpt_weights": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
        "sovits_weights": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
        "ref_audio": "",
        "ref_text": "",
        "ref_lang": "zh",
        "text_lang": "zh",
        "speed": 1.0,
        "temperature": 1.0,
        "top_k": 15,
        "top_p": 1.0,
        "sample_steps": 32,
    },
    # duck_game：说话时把游戏那个进程的音量压低；duck_level 是压到百分之多少
    "audio": {"device": "", "volume": 130, "duck_game": True, "duck_level": 25},
    "overlay": {"enabled": True, "seconds": 8},
    "rice_counter": {"enabled": False, "large_gift_yuan": 10,
                     "overlay_size": 0.16, "overlay_x": None, "overlay_y": None},
    "broadcast": {
        "title": "恶魔之塔 ZERO｜AI 游戏直播", "area_id": 283,
        "auto_stop_minutes": 0,
        "auto_stop_line": "下了下了，吃白饭去了，拜拜～",
        "schedule": {"enabled": False, "mode": "managed", "start_time": "20:00", "stop_time": "23:00",
                     "window_title": "", "audio_name": "", "last_start": "", "last_stop": ""},
    },
    "stream_theme": {
        "preset": "classic", "title": "深蓝放映室", "subtitle": "和你一起，潜入今天的冒险",
        "ticker": "欢迎来到深蓝放映室  ·  发条弹幕，一起聊聊天吧  ·  愿今天的快乐像海浪一样绵长",
        "show_chat": True, "avatar_x": 0.125, "avatar_y": 0.745, "avatar_size": 0.54, "avatar_zoom": 2.0,
    },
    # 当前直播并托管哪个游戏：demontower（恶魔之塔）或 stardew（星露谷物语）
    "game": {"active": "demontower"},
    "stardew": {
        "enabled": True,
        "game_dir": r"E:\Game\xlg\EN00906\Stardew Valley",
        "smapi_exe": "",
        "mcp_host": "127.0.0.1",
        "mcp_port": 8770,
        "openai_base": "",
        "openai_key": "",
        "openai_model": "",
        "persona": "",
        "step_interval": 0.4,
        "auto_launch": True,
        "narrate": True,
        "temperature": 0.6,
        "max_tokens": 400,
        "timeout": 120,
        "tool_timeout": 180,
    },
    "dtgame": {
        "enabled": True,
        "bridge_host": "127.0.0.1",
        "bridge_port": 9787,
        "grans_bridge_port": 9788,
        "openai_base": "",
        "openai_key": "",
        "openai_model": "",
        "character": "",
        "depth": 0,
        "auto_depth": True,
        "narrate": True,
        "step_interval": 0.4,
        "godot_exe": "",
        "project_dir": r"C:\gamedata\新建游戏项目",
        "auto_launch": True,
        "headless": False,
        "persona": (
            "你是一位在B站直播打游戏的主播，正在玩《恶魔之塔 Zero》——"
            "一款类杀戮尖塔的卡牌Roguelike。你说话自然、口语化，像真人边打边念叨。"
        ),
    },
    "wishlist": {
        "enabled": False,
        "action": "audio",
        "audio_file": "",
        "song_query": "",
        "text": "心愿单达成啦！谢谢大家的支持，爱你们！",
        "prompt": "直播间心愿单“{wish}”已经达成，请开心地感谢大家，用两三句话庆祝。",
        "emotion": "开心",
    },
    "songs": {
        "enabled": True,
        "require_gift": False,
        "require_follow": False,
        "song_gifts": "小花花",
        "song_gift_count": 1,
        "pin_gifts": "人气票",
        "pin_gift_count": 1,
        "max_queue": 20,
        "max_per_user": 2,
        "max_minutes": 8,
        "volume": 45,
        "duck": 30,
        "netease_cookie": "",
        "announce_add": True,
        "announce_similar": True,
        "announce_pin": True,
        "announce_play": True,
        "announce_fail": True,
        "announce_need": True,
        "announce_need_follow": True,
        "announce_follow_error": True,
        "announce_full": True,
        "tpl_add": "收到{name}点的《{song}》，排在第{pos}首！",
        "tpl_similar": "原版暂时放不了，给{name}找了相似的《{song}》，排在第{pos}首。",
        "tpl_pin": "{name}的《{song}》插队成功，排到最前面啦！",
        "tpl_play": "下面播放{name}点的《{song}》。",
        "tpl_fail": "抱歉{name}，没找到能播放的《{query}》。",
        "tpl_need": "{name}，送{num}个{gift}就可以点歌哦！",
        "tpl_need_follow": "{name}，先关注主播，再发点歌加歌名就可以免费点歌啦！",
        "tpl_follow_error": "{name}，暂时无法确认关注状态，请稍后再试一次点歌哦！",
        "announce_usage": True,
        "tpl_usage": "发弹幕点歌加歌名就可以点歌啦，比如点歌加晴天！",
        "tpl_full": "{name}，歌单排满啦，等前面的播完再点吧！",
    },
    "fixed_replies": [
        {"enabled": True, "keywords": "晚安", "match": "contains", "reply": "{name}晚安，做个好梦！", "emotion": "开心"},
        {"enabled": True, "keywords": "生日快乐", "match": "contains", "reply": "谢谢{name}！你也要天天开心哦！", "emotion": "感谢"},
    ],
    "live2d": {
        "enabled": False,
        "model_dir": "",
        "model_file": "",
        "scale": 1.0,
        "x": 0.5,
        "y": 0.0,
        "breath": True,
        "blink": True,
        "sway": False,
        "lipsync": True,
        "lip_gain": 1.2,
        "parameter_map": {},
        "face_presets": {},
        "motion_resets": {},
        "idle_motion": "",
        "base_expressions": [],
        "expr_hold": 3,
        "overlay_in_broadcast": True,
        "overlay_corner": "br",
        "overlay_size": 0.5,
        "overlay_margin": 0.02,
        "overlay_x": None,  # 画面中心比例；None 沿用角落和边距
        "overlay_y": None,
        "bubble": True,
        "bubble_x": 0.28,
        "bubble_y": 0.12,
        "bubble_font": 26,
        "bubble_width": 420,
        "bubble_bg": "#ffffff",
        "bubble_color": "#333333",
        "bubble_border": "#fb7299",
        "bubble_hold": 4,
        "emotions": [
            {"name": "开心", "expressions": ["开心兴奋"], "motion": "", "keywords": "哈哈,开心,太好了,好耶,欢迎"},
            {"name": "感谢", "expressions": ["爱心眼"], "motion": "", "keywords": "谢谢,感谢,爱你,比心,破费"},
            {"name": "害羞", "expressions": ["脸红"], "motion": "", "keywords": "害羞,不好意思,讨厌啦,夸我"},
            {"name": "惊讶", "expressions": ["感叹号"], "motion": "", "keywords": "哇,天哪,真的吗,居然,上舰,上船"},
            {"name": "期待", "expressions": ["星星眼"], "motion": "", "keywords": "期待,好想,超想,一起"},
            {"name": "疑惑", "expressions": ["问号"], "motion": "", "keywords": "为什么,咦,什么意思"},
            {"name": "调皮", "expressions": ["吐舌"], "motion": "chuipaopao", "keywords": "嘿嘿,略略略,才不"},
            {"name": "生气", "expressions": ["生气"], "motion": "", "keywords": "哼,生气,气死"},
            {"name": "难过", "expressions": ["悲伤"], "motion": "", "keywords": "难过,伤心,呜呜"},
            {"name": "尴尬", "expressions": ["流汗"], "motion": "", "keywords": "尴尬,呃,汗"},
            {"name": "晕", "expressions": ["晕晕"], "motion": "", "keywords": "晕,头晕,好累"},
            {"name": "无语", "expressions": ["呆呆眼"], "motion": "", "keywords": "无语,啊这"},
        ],
    },
}

_lock = threading.Lock()


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    data = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            os.replace(CONFIG_PATH, CONFIG_PATH + ".broken")
    cfg = _merge(DEFAULTS, data)
    if cfg["ai"].get("persona") == LEGACY_DEFAULT_PERSONA:
        # 只升级旧版出厂人设，保留用户自己写过的性格。
        cfg["ai"]["persona"] = DEFAULT_PERSONA
    if "qwen_model_size" not in data.get("tts", {}):
        # Infer old installations from their actual model, rather than marking
        # a running 1.7B engine as 0.6B after adding the new default field.
        for size in ("1.7B", "0.6B"):
            if any(f"-{size}-" in str(data.get("tts", {}).get(k, "")) for k in ("ttsd_model", "qwen_model")):
                cfg["tts"]["qwen_model_size"] = size
                break
    save(cfg)
    return cfg


def save(cfg):
    with _lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)


def merge(cfg, patch):
    return _merge(cfg, patch)
